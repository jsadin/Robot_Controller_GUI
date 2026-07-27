"""STCM 地图文件解析器。

STCM 是 SLAMTEC 的私有分层地图格式 (Slamtec Composite Map)。
经实机文件 map/202Lab.stcm (Hermes_Pro_Max) 逆向验证, 结构如下:

文件头:
    magic       4 字节  "STCM"
    version     u16 ×4  (实测 18,1,1,1)
    u32         图层数前的保留字段 (实测 8 == 图层数, 但真正图层数在 off=12)
    实际布局:
        off 0  : "STCM"
        off 4  : u16 ×4 版本/标志
        off 12 : u32  图层数 (nlayers)
        off 16 : u16  保留 (0)
        off 18 : 各图层依次排列

每个图层:
    u32  layer_size      (含本字段在内的整层字节数)
    u16  meta_count      元数据条目数
    meta_count × {
        u16 key_len,  key   (ascii)
        u16 val_len,  value (bytes, 可能是 ascii 数字串或二进制)
    }
    payload              紧跟元数据, 长度 = layer_size - (已读字节)

图层通过 metadata['usage'] 区分用途, metadata['type'] 标明 payload 编码:
    explore           grid-map    占用栅格 (渲染核心)
    home_dock_pose    pose-map    充电桩位姿
    point_of_interest pose-map    星标点
    virtual_walls     line-map    虚拟墙
    virtual_tracks    line-map    轨道
    rectangle_area    rect-area   禁区
    pointsmap/landmark            其它

栅格图 (grid-map):
    payload = dimension_width × dimension_height 字节, 行优先, 每格 1 字节占用值。
    metadata: dimension_width / dimension_height / origin_x / origin_y /
              resolution_x / resolution_y (米)。
    占用值实测多样 (0~255): 0 附近=空闲, 高值=障碍, 中间=未知/灰度概率。
"""

from __future__ import annotations

import dataclasses
import struct
from typing import Optional


@dataclasses.dataclass
class StcmLayer:
    usage: str
    type: str
    metadata: dict  # key -> bytes (原始值)
    payload: bytes

    def meta_str(self, key: str, default: str = "") -> str:
        v = self.metadata.get(key)
        return v.decode("latin1") if v is not None else default

    def meta_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.meta_str(key))
        except (ValueError, TypeError):
            return default

    def meta_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.meta_str(key))
        except (ValueError, TypeError):
            return default


@dataclasses.dataclass
class GridMap:
    """占用栅格地图, 可直接喂给渲染层。"""

    width: int
    height: int
    origin_x: float       # 地图坐标系中 (0,0) 格的原点, 米
    origin_y: float
    resolution: float     # 米 / 格
    cells: bytes          # width*height 字节, 行优先

    def occupancy(self, col: int, row: int) -> int:
        return self.cells[row * self.width + col]

    def world_to_cell(self, x: float, y: float) -> tuple[int, int]:
        """世界坐标(米) -> 栅格列行。"""
        col = int(round((x - self.origin_x) / self.resolution))
        row = int(round((y - self.origin_y) / self.resolution))
        return col, row

    def cell_to_world(self, col: int, row: int) -> tuple[float, float]:
        """栅格列行 -> 世界坐标(米)(格中心)。"""
        x = self.origin_x + col * self.resolution
        y = self.origin_y + row * self.resolution
        return x, y


@dataclasses.dataclass
class StcmMap:
    """整张 STCM 地图的解析结果。"""

    version: tuple
    layers: list  # list[StcmLayer]

    def layer(self, usage: str) -> Optional[StcmLayer]:
        for ly in self.layers:
            if ly.usage == usage:
                return ly
        return None

    def grid_map(self) -> Optional[GridMap]:
        """取占用栅格图 (usage=explore 的 grid-map 层)。"""
        for ly in self.layers:
            if "grid-map" in ly.type:
                return GridMap(
                    width=ly.meta_int("dimension_width"),
                    height=ly.meta_int("dimension_height"),
                    origin_x=ly.meta_float("origin_x"),
                    origin_y=ly.meta_float("origin_y"),
                    resolution=ly.meta_float("resolution_x"),
                    cells=ly.payload,
                )
        return None

    def home_dock_pose(self) -> Optional[tuple]:
        """充电桩位姿 (x, y, yaw)。无则 None。"""
        ly = self.layer("home_dock_pose")
        poses = parse_pose_layer(ly) if ly else []
        return poses[0][1:] if poses else None

    def walls(self) -> list:
        """虚拟墙线段 [(id, x1, y1, x2, y2), ...]。"""
        ly = self.layer("virtual_walls")
        return parse_line_layer(ly) if ly else []

    def tracks(self) -> list:
        """轨道线段 [(id, x1, y1, x2, y2), ...]。"""
        ly = self.layer("virtual_tracks")
        return parse_line_layer(ly) if ly else []


def parse_pose_layer(layer: "StcmLayer") -> list:
    """解析 pose-map 层 payload -> [(name, x, y, yaw), ...]。

    单条目格式经 home_dock 实测:
        u16 name_len, name, 1 字节 flag(0x00), float x, y, yaw, 3 字节尾部。
    注: 本样本中 POI/landmark 层为空, 多条目布局未实证, 上层拿到实机
    含多条 POI 的地图后需再校验。安全起见这里只在长度足够时尽力解析。
    """
    pl = layer.payload
    out = []
    p = 0
    n = len(pl)
    while p + 2 <= n:
        nl = struct.unpack_from("<H", pl, p)[0]
        p += 2
        if p + nl + 1 + 12 > n:
            break
        name = pl[p : p + nl].decode("latin1")
        p += nl
        p += 1  # flag
        x, y, yaw = struct.unpack_from("<3f", pl, p)
        p += 12
        # 尾部对齐字节 (实测 home_dock 有 3 字节); 容错跳过到下一条
        out.append((name, x, y, yaw))
        # 若还有剩余但不足一条, 停止
        if p + 2 > n or all(b == 0 for b in pl[p:]):
            break
    return out


def parse_line_layer(layer: "StcmLayer") -> list:
    """解析 line-map 层 payload -> [(id, x1, y1, x2, y2), ...]。

    用于 virtual_walls(虚拟墙) 与 virtual_tracks(轨道)。
    单条目格式经 virtual_walls 实测:
        u16 id_len, id, float x1, y1, x2, y2, 2 字节尾(flag, 实测 0x0000)。
    """
    pl = layer.payload
    out = []
    p = 0
    n = len(pl)
    while p + 2 <= n:
        idl = struct.unpack_from("<H", pl, p)[0]
        p += 2
        if p + idl + 16 + 2 > n:
            break
        line_id = pl[p : p + idl].decode("latin1")
        p += idl
        x1, y1, x2, y2 = struct.unpack_from("<4f", pl, p)
        p += 16
        p += 2  # flag 尾部
        out.append((line_id, x1, y1, x2, y2))
    return out



def parse_stcm(data: bytes) -> StcmMap:
    """解析 STCM 二进制数据 -> StcmMap。

    既可解析本地 .stcm 文件, 也可解析 HermesClient.get_map_stcm() 的返回。
    """
    if data[:4] != b"STCM":
        raise ValueError("不是有效的 STCM 文件 (magic 不匹配)")

    version = struct.unpack_from("<4H", data, 4)
    off = 12
    nlayers = struct.unpack_from("<I", data, off)[0]
    off += 4
    off += 2  # u16 保留字段 (实测为 0)

    layers: list[StcmLayer] = []
    for _ in range(nlayers):
        lstart = off
        layer_size = struct.unpack_from("<I", data, off)[0]
        meta_count = struct.unpack_from("<H", data, off + 4)[0]
        p = off + 6
        meta: dict[str, bytes] = {}
        for _ in range(meta_count):
            kl = struct.unpack_from("<H", data, p)[0]
            p += 2
            key = data[p : p + kl].decode("latin1")
            p += kl
            vl = struct.unpack_from("<H", data, p)[0]
            p += 2
            meta[key] = data[p : p + vl]
            p += vl
        payload = data[p : lstart + layer_size]
        layers.append(
            StcmLayer(
                usage=meta.get("usage", b"").decode("latin1"),
                type=meta.get("type", b"").decode("latin1"),
                metadata=meta,
                payload=payload,
            )
        )
        off = lstart + layer_size

    return StcmMap(version=version, layers=layers)


def parse_stcm_file(path: str) -> StcmMap:
    with open(path, "rb") as f:
        return parse_stcm(f.read())
