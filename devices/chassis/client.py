"""SLAMWARE Robot Agent REST 客户端。

所有接口基于手册 "Slamware RESTful API Development Manual V1.1" 与
Hermes 48V 用户手册第十一章。基址默认 http://<ip>:1448。

设计原则:
- 这是一层"薄封装"：每个方法对应一个 REST 端点，只做参数拼装和
  返回值解析，不掺业务逻辑(任务编排/调度放在更上层)。
- 网络/HTTP 错误统一抛 HermesError，方便上层界面捕获后弹提示。
"""

from __future__ import annotations

import dataclasses
import math
import uuid
from typing import Any, Optional

import requests


class HermesError(Exception):
    """底盘通信或接口返回异常。"""


# --------- 数据结构 (轻量, 只放常用字段, 原始 dict 也一并保留) ---------


@dataclasses.dataclass
class Pose:
    """机器人在地图坐标系中的位姿, 单位: 米 / 弧度。"""

    x: float
    y: float
    yaw: float  # 朝向, 弧度

    @classmethod
    def from_dict(cls, d: dict) -> "Pose":
        return cls(x=d.get("x", 0.0), y=d.get("y", 0.0), yaw=d.get("yaw", 0.0))


@dataclasses.dataclass
class LaserScan:
    """一帧激光扫描 (功能表雷达可视化)。

    schema(实机 swagger 确认): {pose, laser_points:[{distance,angle,valid}]}。
    坐标系: pose 是观测该帧时的机器人位姿; 每个点的 angle 是激光与机器人
    正前方的夹角(弧度), distance 为距离(米) —— 即机器人本体极坐标。

    world_points() 用观测 pose 把点转换到地图世界坐标, 供画布直接渲染。
    """

    pose: "Pose"
    points: list  # list[(distance, angle, valid)]

    @classmethod
    def from_dict(cls, d: dict) -> "LaserScan":
        pose = Pose.from_dict(d.get("pose", {}) or {})
        pts = []
        for p in d.get("laser_points", []) or []:
            pts.append((
                p.get("distance", 0.0),
                p.get("angle", 0.0),
                p.get("valid", True),
            ))
        return cls(pose=pose, points=pts)

    def world_points(self) -> list:
        """转换为世界坐标点列表 [(x, y), ...](仅含 valid 点)。

        点在世界系: 机器人位置 + 距离沿(机器人朝向 + 点角度)方向。
        """
        out = []
        px, py, pyaw = self.pose.x, self.pose.y, self.pose.yaw
        for dist, ang, valid in self.points:
            if not valid or dist <= 0:
                continue
            theta = pyaw + ang
            out.append((px + dist * math.cos(theta),
                        py + dist * math.sin(theta)))
        return out


@dataclasses.dataclass
class PowerStatus:
    """电量与充电状态, 对应 GET power/status。"""

    battery_percentage: int
    is_charging: bool
    is_dc_connected: bool
    docking_status: str  # on_dock / not_on_dock ...
    power_stage: str
    sleep_mode: str
    raw: dict

    @classmethod
    def from_dict(cls, d: dict) -> "PowerStatus":
        return cls(
            battery_percentage=d.get("batteryPercentage", 0),
            is_charging=d.get("isCharging", False),
            is_dc_connected=d.get("isDCConnected", False),
            docking_status=d.get("dockingStatus", ""),
            power_stage=d.get("powerStage", ""),
            sleep_mode=d.get("sleepMode", ""),
            raw=d,
        )


@dataclasses.dataclass
class POI:
    """兴趣点(星标), 对应 artifact/pois。"""

    poi_id: str
    name: str
    x: float
    y: float
    yaw: float
    raw: dict

    @classmethod
    def from_dict(cls, d: dict) -> "POI":
        pose = d.get("pose", {}) or {}
        meta = d.get("metadata", {}) or {}
        return cls(
            poi_id=str(d.get("id", "")),
            name=meta.get("display_name") or meta.get("name") or d.get("id", ""),
            x=pose.get("x", 0.0),
            y=pose.get("y", 0.0),
            yaw=pose.get("yaw", 0.0),
            raw=d,
        )


# ----------------------------- 客户端 -----------------------------

# Action 工厂名, 创建运动行为时作为 action_name 传入。
# 注: 前缀为 "agent.actions." (经实机 Hermes_Pro_Max 固件 6.3.2 验证,
# 手册里写的 "slamtec." 前缀在本固件上不适用)。
ACTION_MOVE_TO = "agent.actions.MoveToAction"
ACTION_SERIES_MOVE_TO = "agent.actions.SeriesMoveToAction"
ACTION_MOVE_BY = "agent.actions.MoveByAction"  # 遥控, 需周期调用
ACTION_GO_HOME = "agent.actions.GoHomeAction"
ACTION_ROTATE_TO = "agent.actions.RotateToAction"
ACTION_FOLLOW_PATH = "agent.actions.FollowPathPointsAction"  # 沿固定路线
ACTION_ROTATE = "agent.actions.RotateAction"

# MoveByAction 的方向整数枚举 (实机已确认 direction=0 可用)。
DIR_FORWARD = 0
DIR_BACKWARD = 1
DIR_TURN_LEFT = 2
DIR_TURN_RIGHT = 3

# MoveOptions.mode (实机 spec 确认): 虚拟轨道行进模式。
MOVE_MODE_FREE = 0          # 自由导航(忽略虚拟轨道)
MOVE_MODE_TRACK_STRICT = 1  # 严格轨道(遇障碍停车等待)
MOVE_MODE_TRACK_FIRST = 2   # 轨道优先(遇障碍绕开轨迹再回归)


class HermesClient:
    """Hermes 底盘 REST 客户端。

    用法::

        c = HermesClient("192.168.11.1")
        c.ping()
        print(c.get_power_status().battery_percentage)
    """

    def __init__(self, host: str, port: int = 1448, timeout: float = 5.0):
        self.base = f"http://{host}:{port}"
        self.timeout = timeout
        self._session = requests.Session()
        # 默认导航模式(MoveOptions.mode); UI"轨道优先"开关统一改这里,
        # 所有 move_to/series_move_to(含任务执行器)默认遵循。
        self.move_mode = MOVE_MODE_FREE

    # ---- 底层请求 ----

    def _request(self, method: str, path: str, **kw) -> Any:
        url = self.base + path
        kw.setdefault("timeout", self.timeout)
        try:
            resp = self._session.request(method, url, **kw)
        except requests.RequestException as e:
            raise HermesError(f"请求失败 {method} {path}: {e}") from e
        if not resp.ok:
            raise HermesError(
                f"{method} {path} 返回 {resp.status_code}: {resp.text[:200]}"
            )
        if not resp.content:
            return None
        ctype = resp.headers.get("Content-Type", "")
        if "application/json" in ctype:
            return resp.json()
        return resp.content  # 二进制(如 stcm 地图)

    def _get(self, path: str, **kw) -> Any:
        return self._request("GET", path, **kw)

    def _post(self, path: str, json: Optional[dict] = None, **kw) -> Any:
        return self._request("POST", path, json=json, **kw)

    def _put(self, path: str, json: Optional[dict] = None, **kw) -> Any:
        return self._request("PUT", path, json=json, **kw)

    def _delete(self, path: str, **kw) -> Any:
        return self._request("DELETE", path, **kw)

    # ---- 连通性 / 系统信息 (功能表 #15 日志) ----

    def ping(self) -> dict:
        """读机器人基本信息, 作为连通性探测。成功即说明底盘可达。"""
        return self._get("/api/core/system/v1/robot/info")

    def get_capabilities(self) -> dict:
        return self._get("/api/core/system/v1/capabilities")

    def get_health(self) -> dict:
        """健康状态原始 dict(碰撞/急停/定位质量低/错误码等)。"""
        return self._get("/api/core/system/v1/robot/health")

    def get_health_items(self) -> dict:
        """解析健康状态为易用结构(实机字段已确认)。

        返回 {
            "errors": [{"code","message","level"}],  # baseError 列表抽取
            "has_error", "has_warning", "has_fatal",
            "lidar_disconnected", "emergency_stop",
        }
        level: 1=警告 2=错误 3=致命。
        """
        raw = self.get_health() or {}
        items = []
        for e in raw.get("baseError", []) or []:
            items.append({
                "code": e.get("errorCode"),
                "message": e.get("message", ""),
                "level": e.get("level", 0),
            })
        return {
            "errors": items,
            "has_error": raw.get("hasError", False),
            "has_warning": raw.get("hasWarning", False),
            "has_fatal": raw.get("hasFatal", False),
            "lidar_disconnected": raw.get("hasLidarDisconnected", False),
            "emergency_stop": raw.get("hasSystemEmergencyStop", False),
        }

    def clear_health(self, error_code) -> None:
        """清除某条健康/报警记录(对标 RoboStudio "移除")。

        注: 仅 dismiss 记录, 若根因未解决底盘会重新报。
        实机 swagger 确认: DELETE robot/health/{error_code}。
        """
        self._delete(f"/api/core/system/v1/robot/health/{error_code}")

    def get_power_status(self) -> PowerStatus:
        """电量与充电状态(功能表 #15 电量显示)。"""
        return PowerStatus.from_dict(self._get("/api/core/system/v1/power/status"))

    def shutdown(self, restart: bool = False) -> None:
        """关机或重启(功能表 餐厅 App 的关闭/重启)。高风险, 上层应二次确认。"""
        self._post("/api/core/system/v1/power/:shutdown", json={"restart": restart})

    # ---- 定位 / 位姿 (功能表 #2 实时位置, #16 激光定位) ----

    def get_pose(self) -> Pose:
        """获取机器人当前位姿。地图渲染时周期调用此接口刷新机器人位置。

        注: 不同固件版本 pose 路径可能为
        /api/core/slam/v1/localization/pose 或 multi-floor 版本,
        这里优先用 slam 路径, 失败再回退。
        """
        try:
            d = self._get("/api/core/slam/v1/localization/pose")
        except HermesError:
            d = self._get("/api/multi-floor/localization/v1/pose")
        return Pose.from_dict(d)

    def set_pose(self, x: float, y: float, yaw: float) -> None:
        """手动重定位: 把机器人强制设置到地图中的某个位置(治本定位质量低)。

        实机 swagger 确认: PUT /api/core/slam/v1/localization/pose (setPose),
        body 是 Pose3D。用于被推动/定位质量低后, 告诉底盘真实位姿重新匹配。
        """
        self._put(
            "/api/core/slam/v1/localization/pose",
            json={"x": x, "y": y, "z": 0, "yaw": yaw, "pitch": 0, "roll": 0},
        )

    def set_pose_by_poi(self, poi_name: str) -> None:
        """重定位到某个已命名 POI 上(setPoseByPOI)。备用。"""
        self._put(
            "/api/multi-floor/localization/v1/pose",
            json={"poi_name": poi_name},
        )

    def set_localization(self, enable: bool) -> None:
        """开启/暂停定位(功能表 #16 激活激光定位)。"""
        self._put("/api/core/slam/v1/localization/:enable", json={"enable": enable})

    def get_laser_scan(self) -> LaserScan:
        """获取当前激光扫描帧(实机 swagger 确认路径在 system 模块下)。

        用于雷达点云实时可视化。返回含观测位姿与本体系极坐标点,
        LaserScan.world_points() 可直接转世界坐标渲染。
        """
        return LaserScan.from_dict(self._get("/api/core/system/v1/laserscan"))

    def get_localization_quality(self) -> int:
        """定位质量/置信度(0~100), 用于任务前置健康检查。"""
        return self._get("/api/core/slam/v1/localization/quality")

    def set_mapping(self, enable: bool) -> None:
        """开启/暂停建图。"""
        self._put("/api/core/slam/v1/mapping/:enable", json={"enable": enable})

    # ---- 地图 (功能表 #1 显示地图) ----

    def get_map_stcm(self) -> bytes:
        """获取 STCM 格式复合地图(二进制)。用于地图渲染或保存。"""
        return self._get("/api/core/slam/v1/maps/stcm")

    def get_home_pose(self) -> Pose:
        """获取充电桩位置(功能表 #3 充电桩为原点)。"""
        d = self._get("/api/core/slam/v1/homepose")
        return Pose.from_dict(d)

    def set_home_pose(self, x: float, y: float, yaw: float) -> None:
        """设置充电桩位置(功能表 #3 自主设置充电桩)。"""
        self._put(
            "/api/core/slam/v1/homepose",
            json={"x": x, "y": y, "yaw": yaw},
        )

    # ---- POI / 星标 (功能表 #9 #10) ----

    def list_pois(self) -> list[POI]:
        """获取地图中所有 POI(星标点)。"""
        data = self._get("/api/core/artifact/v1/pois") or []
        return [POI.from_dict(d) for d in data]

    def add_poi(self, name: str, x: float, y: float, yaw: float = 0.0) -> dict:
        """添加一个星标点。坐标由 SDK 自动持久化, 无需手动导入导出(功能表 #9)。

        手册要求: 调用方自行生成 UUID 作为 id, metadata 含 display_name 与 type。
        返回创建后的 POI(含 id), 便于上层直接拿到 id。
        """
        poi_id = str(uuid.uuid4())
        body = {
            "id": poi_id,
            "pose": {"x": x, "y": y, "yaw": yaw},
            "metadata": {"display_name": name, "type": "poi"},
        }
        resp = self._post("/api/core/artifact/v1/pois", json=body)
        # 部分固件返回 204 无内容, 此时回填我们生成的 id
        if not resp:
            return {"id": poi_id, **body}
        return resp

    def delete_poi(self, poi_id: str) -> None:
        self._delete(f"/api/core/artifact/v1/pois/{poi_id}")

    def update_poi(self, poi_id: str, name: str,
                   x: float, y: float, yaw: float = 0.0) -> dict:
        """修改星标(主要用于调整朝向 yaw, 也可改名/位置)。

        端点 PUT /api/core/artifact/v1/pois/{poi_id}, body 同 add。
        未连机, 若 PUT 不接受, 按实机返回校正(大概率 body 与 add 一致)。
        """
        body = {
            "id": poi_id,
            "pose": {"x": x, "y": y, "yaw": yaw},
            "metadata": {"display_name": name, "type": "poi"},
        }
        resp = self._put(f"/api/core/artifact/v1/pois/{poi_id}", json=body)
        return resp if resp else {"id": poi_id, **body}

    # ---- 虚拟墙 / 禁区 (功能表 #7) ----

    def list_walls(self) -> list[dict]:
        """获取所有虚拟墙线段。usage=walls。

        返回对象数组, 每条含 {id, start{x,y}, end{x,y}, metadata}。
        """
        return self._get("/api/core/artifact/v1/lines/walls") or []

    def delete_wall(self, wall_id) -> None:
        """按 id 删除一条虚拟墙(功能表 #7 拆除虚拟墙)。

        id 取自 list_walls() 返回的数值 id。实机已验证 DELETE 可用。
        """
        self._delete(f"/api/core/artifact/v1/lines/walls/{wall_id}")

    def add_wall(self, x1: float, y1: float, x2: float, y2: float) -> dict:
        """添加一条虚拟墙线段(功能表 #7 标定障碍)。

        实机已确认: body 是顶层数组, 每条线段为 {"start":{x,y},"end":{x,y}};
        不要 lines 包裹, 不要 id 字段(添加时 id 无效)。成功返回 true。
        """
        body = [{"start": {"x": x1, "y": y1}, "end": {"x": x2, "y": y2}}]
        return self._post("/api/core/artifact/v1/lines/walls", json=body)

    # ---- 虚拟轨道 (line-map, REST usage=tracks; STCM 层名 virtual_tracks) ----
    # 注: 墙的 REST usage 是 walls(非 virtual_walls), 轨道同理是 tracks。

    def list_tracks(self) -> list[dict]:
        """获取所有虚拟轨道线段。返回对象数组(同墙: id/start/end)。"""
        return self._get("/api/core/artifact/v1/lines/tracks") or []

    def add_track(self, points: list) -> dict:
        """添加一条多点折线轨道。points: [(x,y), ...] 折线顶点。

        折线拆成相邻顶点的多个 {start,end} 段, 一次顶层数组提交
        (body 与墙同构, 墙实测顶层数组可用)。
        """
        segs = []
        for i in range(len(points) - 1):
            (x1, y1), (x2, y2) = points[i], points[i + 1]
            segs.append({"start": {"x": x1, "y": y1},
                         "end": {"x": x2, "y": y2}})
        return self._post("/api/core/artifact/v1/lines/tracks", json=segs)

    def delete_track(self, track_id) -> None:
        """按 id 删除一条轨道线段。"""
        self._delete(f"/api/core/artifact/v1/lines/tracks/{track_id}")

    def add_forbidden_area(
        self, x: float, y: float, w: float, h: float
    ) -> dict:
        """添加矩形禁区(功能表 #7)。依赖 Forbidden Area 插件已安装。"""
        body = {
            "area": {"center": {"x": x, "y": y}, "width": w, "height": h},
        }
        return self._post(
            "/api/core/artifact/v1/rectangle-areas/forbidden", json=body
        )

    # ---- 运动控制 / Action (功能表 #6 遥控, #8 轨道, #11/#13 调度) ----

    def list_action_factories(self) -> list[str]:
        """获取底盘支持的所有 Action 类型名(已抽出 action_name 字段)。"""
        data = self._get("/api/core/motion/v1/action-factories") or []
        names = []
        for item in data:
            if isinstance(item, dict):
                names.append(item.get("action_name", ""))
            else:
                names.append(str(item))
        return names

    def create_action(self, action_name: str, options: dict) -> dict:
        """创建一个运动行为(底层方法)。返回含 action_id 的 dict。

        同一时刻底盘只执行一个 action, 新建会打断/替换当前 action。
        """
        body = {"action_name": action_name, "options": options}
        return self._post("/api/core/motion/v1/actions", json=body)

    def move_to(
        self,
        x: float,
        y: float,
        yaw: float = 0.0,
        precision: float = 0.0,
        retry: int = 0,
        mode: Optional[int] = None,
        apply_yaw: bool = False,
        directed_track: bool = False,
    ) -> dict:
        """自主导航到指定坐标(功能表 #11 单点调度的底层调用)。

        mode: MoveOptions.mode(实机 spec 确认)。
            0 自由导航 / 1 严格轨道(遇障停等) / 2 轨道优先(遇障绕行)。
            None 时取实例默认 self.move_mode(由 UI"轨道优先"开关统一设置),
            使点击导航/星标前往/任务执行都遵循同一模式。
        apply_yaw: 仅当为 True 才附加 with_yaw flag, 到点朝向 yaw 才生效;
            否则底盘忽略 yaw(实机 spec 明确: yaw 需配合 with_yaw)。
        directed_track: 附加 with_directed_virtual_track flag(沿有向虚拟轨道)。
        """
        flags: list[str] = []
        if apply_yaw:
            flags.append("with_yaw")
        if directed_track:
            flags.append("with_directed_virtual_track")
        options = {
            "target": {"x": x, "y": y, "z": 0},
            "move_options": {
                "mode": self.move_mode if mode is None else mode,
                "flags": flags,
                "yaw": yaw,
                "acceptable_precision": precision,
                "fail_retry_count": retry,
            },
        }
        return self.create_action(ACTION_MOVE_TO, options)

    def move_to_poi(self, poi: POI, **kw) -> dict:
        """导航到某个星标点(功能表 #11)。"""
        return self.move_to(poi.x, poi.y, poi.yaw, **kw)

    @staticmethod
    def _parse_path_points(d) -> list:
        """解析 path_points 为 [(x,y), ...]。兼容 {path_points:[[x,y],...]}
        或 [[x,y],...] 或 [{x,y},...]。"""
        pts = d.get("path_points", d) if isinstance(d, dict) else d
        out = []
        for p in pts or []:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                out.append((p[0], p[1]))
            elif isinstance(p, dict):
                out.append((p.get("x", 0.0), p.get("y", 0.0)))
        return out

    def search_path(self, x: float, y: float, timeout: int = 5000) -> list:
        """规划从机器人到目标点的路径(不移动)。返回 [(x,y),...], 无路径返回 []。

        实机已验证: POST :search_path, 返回 {path_points:[[x,y],...]}。
        """
        try:
            d = self._post("/api/core/motion/v1/:search_path",
                           json={"target": {"x": x, "y": y}, "timeout": timeout})
        except HermesError:
            return []
        return self._parse_path_points(d)

    def get_remaining_path(self) -> list:
        """当前 action 的剩余路径点 [(x,y),...]。无 action/无路径返回 []。"""
        try:
            d = self._get("/api/core/motion/v1/path")
        except HermesError:
            return []
        return self._parse_path_points(d)

    def series_move_to(self, points: list[tuple[float, float]],
                       mode: Optional[int] = None) -> dict:
        """按顺序导航经过多个点(功能表 #8 既定路线 / #13 多条调度)。

        points: [(x, y), ...]
        mode: 同 move_to, None 取实例默认 self.move_mode。
        """
        options = {
            "targets": [{"x": x, "y": y, "z": 0} for x, y in points],
            "move_options": {
                "mode": self.move_mode if mode is None else mode,
                "flags": [],
            },
        }
        return self.create_action(ACTION_SERIES_MOVE_TO, options)

    def move_by(self, direction: int, speed_ratio: float = 0.0,
               linear_velocity: float = 0.0,
               angular_velocity: float = 0.0) -> dict:
        """方向遥控(功能表 #6 方向键前后左右)。

        direction: 整数方向枚举(实机已确认)
            0=前进  1=后退  2=左转  3=右转
        speed_ratio: 速度比例(0~1), 部分固件支持; 0=默认。
        linear_velocity: 绝对线速度 m/s, 部分固件支持(前进/后退方向生效)。
        angular_velocity: 绝对角速度 rad/s, 部分固件支持(转向方向生效)。

        注: 不同固件对速度字段的支持不一致; 同时传多个字段由固件自行选用,
        以 linear/angular_velocity 为优先(绝对值更可靠)。

        MoveByAction 需要被周期性(如每 100~200ms)重复调用才能持续运动,
        松开按键即停止调用并 cancel, 底盘随之停下。
        """
        options: dict = {"direction": direction}
        if speed_ratio:
            options["speed_ratio"] = speed_ratio
        if linear_velocity > 0:
            options["linear_velocity"] = round(linear_velocity, 4)
        if angular_velocity > 0:
            options["angular_velocity"] = round(angular_velocity, 4)
        return self.create_action(ACTION_MOVE_BY, options)

    def go_home(self) -> dict:
        """自主回充电桩(功能表 餐厅 App 回桩)。"""
        return self.create_action(ACTION_GO_HOME, {})

    def rotate_to(self, yaw: float) -> dict:
        """原地旋转到指定朝向(弧度)。

        实机 spec 确认 RotateToActionOptions 只有一个字段 angle(number, 弧度),
        不是 {"orientation":{"yaw":...}}。旧格式会被底盘 400 拒绝, 导致到点补转
        静默失效(异常被上层吞掉)。
        """
        return self.create_action(ACTION_ROTATE_TO, {"angle": yaw})

    def get_current_action(self) -> Optional[dict]:
        """获取当前正在执行的 action 状态(功能表 #15 指令显示)。无则返回 None。"""
        try:
            return self._get("/api/core/motion/v1/actions/:current")
        except HermesError:
            return None

    def get_action(self, action_id: str) -> dict:
        """按 ID 查询某个 action 的状态(用于轮询任务是否完成)。"""
        return self._get(f"/api/core/motion/v1/actions/{action_id}")

    def cancel_current_action(self) -> None:
        """终止当前行为(功能表 #14 中断任务 / 急停后)。"""
        self._delete("/api/core/motion/v1/actions/:current")

    # ---- 运动策略 / 速度 (功能表 #5 调节速度) ----
    # 实机已确认: 策略有 default/depot/agile/delivery/inventory/low_speed;
    # base.max_moving_speed(线速度,默认0.7)、base.max_angular_speed(角速度,
    # 默认1.2) 均可读可设。

    # 已知运动策略 (实机 Hermes_Pro_Max 返回)
    STRATEGIES = ["default", "depot", "agile", "delivery", "inventory", "low_speed"]

    def get_strategies(self) -> list[dict]:
        return self._get("/api/core/motion/v1/strategies") or []

    def get_current_strategy(self) -> dict:
        return self._get("/api/core/motion/v1/strategies/:current")

    def set_strategy(self, strategy_id: str) -> None:
        """切换运动策略(影响速度/避障行为, 功能表 #5)。"""
        self._put(
            "/api/core/motion/v1/strategies/:current",
            json={"id": strategy_id},
        )

    def get_parameter(self, param: str) -> str:
        """读系统参数(返回字符串, 如 '0.700000')。"""
        raw = self._get(f"/api/core/system/v1/parameter?param={param}")
        if raw is None:
            return ""
        # JSON 可能是带引号字符串或裸值
        if isinstance(raw, (int, float)):
            return str(raw)
        s = str(raw).strip()
        if len(s) >= 2 and s[0] == s[-1] == '"':
            s = s[1:-1]
        return s

    def set_parameter(self, param: str, value) -> None:
        self._put(
            "/api/core/system/v1/parameter",
            json={"param": param, "value": str(value)},
        )

    def set_emergency_stop(self, on: bool) -> None:
        """远程急停。on=触发急停(立即停止且不再响应运动指令), off=解除。

        实机参数 base.emergency_stop, value on/off。安全相关, UI 应二次确认。
        """
        self.set_parameter("base.emergency_stop", "on" if on else "off")

    def set_brake_release(self, on: bool) -> None:
        """刹车释放。on=释放刹车(可人为推动底盘), off=恢复刹车制动。

        实机参数 base.brake_release, value on/off。
        """
        self.set_parameter("base.brake_release", "on" if on else "off")

    def get_max_speed(self) -> float:
        """读最大线速度 m/s。"""
        return float(self.get_parameter("base.max_moving_speed"))

    def set_max_speed(self, speed: float) -> None:
        """设最大线速度 m/s (功能表 #5)。参数名已实机确认。"""
        self.set_parameter("base.max_moving_speed", f"{speed:.6f}")

    def get_max_angular_speed(self) -> float:
        """读最大角速度 rad/s。"""
        return float(self.get_parameter("base.max_angular_speed"))

    def set_max_angular_speed(self, speed: float) -> None:
        """设最大角速度 rad/s。"""
        self.set_parameter("base.max_angular_speed", f"{speed:.6f}")
