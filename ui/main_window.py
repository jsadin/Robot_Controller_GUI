"""上位机主窗口 (阶段 1-2: 地图可视化 + 实时位姿 + 星标调度)。

功能:
    - 载入本地 .stcm 文件 或 从底盘在线拉取地图
    - 渲染占用栅格 + 充电桩
    - 连接底盘后, 定时轮询 pose/电量并实时刷新机器人位置(功能表 #2 #15)
    - 视角: 适应窗口 / 跟随机器人 (功能表 #4)
    - 星标: 地图点击添加 / 列表前往 / 删除 (功能表 #9 #10 #11)

运行:
    python -m ui.main_window           # 离线, 默认加载 map/202Lab.stcm
    python -m ui.main_window <底盘IP>   # 连接实机
"""

from __future__ import annotations

import sys
from datetime import datetime

# 必须在 import PyQt5 之前加载 elite_cs_sdk，否则 Windows 上会与 Qt 发生
# 原生库冲突（进程直接 0xC0000005 退出，点「连接」后进不了主界面）。
try:
    import elite_cs_sdk  # noqa: F401
except ImportError:
    pass

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from devices.chassis import HermesClient, HermesError
from devices.chassis.client import MOVE_MODE_FREE, MOVE_MODE_TRACK_FIRST
from devices.chassis.stcm import parse_stcm, parse_stcm_file
from devices.config_loader import DevicesConfig, load_devices_config
from devices.arm import ArmController
from devices.camera import build_camera, save_snapshot
from devices.ranging import build_ranging
from devices.common import EStopBus
from core.diagnosis import DiagnosisAggregator
from core.mission import MissionExecutor, MissionStore
from ui.map_canvas import MapCanvas
from ui.poi_panel import PoiPanel
from ui.control_panel import ControlPanel
from ui.task_panel import TaskPanel
from ui.task_dialog import TaskDialog
from ui.arm_panel import ArmPanel
from ui.arm_worker import ArmControlWorker
from ui.vision_panel import VisionPanel
from ui.diagnosis_panel import DiagnosisPanel
from ui.mission_panel import MissionPanel
from ui.action_sequences_dialog import ActionSequencesDialog
from ui.workspace import MainWorkspace
from tasks import TaskStore, TaskExecutor, Task, TaskStatus
from tasks.scheduler import check_due


class MainWindow(QMainWindow):
    def __init__(self, host: str = "", port: int = 1448,
                 map_file: str = "map/202Lab.stcm",
                 devices_cfg: DevicesConfig | None = None):
        super().__init__()
        self.setWindowTitle("机器人控制器 — 整合版")
        self.resize(1440, 900)
        self._port = port
        self.cfg = devices_cfg or load_devices_config()
        self.cfg.ensure_data_dir()

        self.client: HermesClient | None = None
        self.arm = ArmController(self.cfg)
        self.camera = build_camera(self.cfg)
        self.ranging = build_ranging(self.cfg)
        self.estop_bus = EStopBus()
        self.estop_bus.bind(arm=self.arm, stop_mission=lambda: None)
        self.arm_worker = ArmControlWorker(self.arm)
        self.arm_worker.on_joints = self._on_arm_joints_from_worker
        self.arm_worker.start()
        self.mission_store = MissionStore(self.cfg.data_dir / "missions.db")
        self.mission_exec = MissionExecutor(
            chassis=None, arm=self.arm, camera=self.camera,
            data_dir=self.cfg.data_dir,
            on_status=lambda s: self.status(s),
        )
        self.estop_bus.bind(stop_mission=self.mission_exec.abort)
        self.diag = DiagnosisAggregator(
            chassis=None, arm=self.arm, camera=self.camera, ranging=self.ranging
        )

        self.canvas = MapCanvas()
        self.panel = PoiPanel()
        self.control = ControlPanel()
        self.task_panel = TaskPanel()
        self.arm_panel = ArmPanel()
        self.vision_panel = VisionPanel()
        self.diagnosis_panel = DiagnosisPanel()
        self.mission_panel = MissionPanel()
        self._pois: list = []          # 当前星标缓存 (POI 对象)
        self._goto_name = ""           # 正在前往的星标名(状态栏显示用)
        self._goto_yaw = None          # 到点后需补转的目标朝向(弧度), None=不补转

        # 任务业务层 (纯逻辑, 不碰 Qt)
        self.store = TaskStore()
        self.executor = TaskExecutor(
            self.store,
            pois_provider=lambda: self._pois,
            on_change=self.on_task_change,
        )
        self._last_sched_check = None   # 上次调度检查的分钟, 防同分钟重复
        self._status_pinned_until = 0.0  # 重要状态消息的固定截止时间(monotonic)

        # 顶部工具条
        self.status_light = QLabel("●")
        self.status_light.setToolTip("连接状态")
        self.conn_label = QLabel("未连接")
        self.ip_edit = QLineEdit(host or "192.168.11.1")
        self.ip_edit.setFixedWidth(120)
        self.port_edit = QLineEdit(str(port))
        self.port_edit.setFixedWidth(56)
        self.btn_connect = QPushButton("🔗 连接")
        self.btn_connect.setObjectName("primary")
        self.btn_load = QPushButton("🗁 载入地图")
        self.btn_pull = QPushButton("⭳ 拉取地图")
        self.btn_wall = QPushButton("✎ 画墙")
        self.btn_wall_mgr = QPushButton("☰ 管理墙")
        self.btn_track = QPushButton("〜 画轨道")
        self.btn_track_mgr = QPushButton("☰ 管理轨道")
        self.btn_home = QPushButton("⌂ 回桩")
        self.btn_reloc = QPushButton("⌖ 重定位")
        self.btn_nav = QPushButton("🧭 导航")
        self.btn_health = QPushButton("⚠ 健康")
        self.btn_fit = QPushButton("⛶ 适应窗口")
        self.btn_overview = QPushButton("总览")
        self.btn_overview.setObjectName("primary")
        self.btn_overview.setToolTip("恢复地图+底部分屏总览布局")
        self.btn_max_map = QPushButton("放大地图")
        self.btn_max_map.setToolTip("地图铺满主工作区")
        self.chk_follow = QCheckBox("跟随机器人")
        self.chk_laser = QCheckBox("显示雷达")
        self.chk_track_first = QCheckBox("轨道优先")
        self.chk_track_first.setToolTip(
            "勾选后导航走虚拟轨道(遇障碍绕开轨迹再回归); "
            "影响点击导航、星标前往与任务执行")
        self._set_conn_light(False)

        self.btn_connect.clicked.connect(self.on_connect)
        self.btn_load.clicked.connect(self.on_load_file)
        self.btn_pull.clicked.connect(self.on_pull_map)
        self.btn_wall.clicked.connect(self.on_wall_mode)
        self.btn_wall_mgr.clicked.connect(self.on_manage_walls)
        self.btn_home.clicked.connect(self.on_go_home)
        self.btn_reloc.clicked.connect(self.on_reloc_mode)
        self.btn_health.clicked.connect(self.on_show_health)
        self.btn_fit.clicked.connect(self.canvas.fit_view)
        self.btn_overview.clicked.connect(self.on_workspace_overview)
        self.btn_max_map.clicked.connect(self.on_workspace_max_map)
        self.chk_follow.toggled.connect(self.canvas.set_follow_robot)
        self.chk_laser.toggled.connect(self.on_laser_toggle)
        self.chk_track_first.toggled.connect(self.on_track_first_toggle)
        self.canvas.wallDrawn.connect(self.on_wall_drawn)
        self.canvas.relocRequested.connect(self.on_reloc_done)
        self.btn_track.clicked.connect(self.on_track_mode)
        self.btn_track_mgr.clicked.connect(self.on_manage_tracks)
        self.canvas.trackDrawn.connect(self.on_track_drawn)
        self.canvas.poiHeadingChanged.connect(self.apply_poi_yaw_rad)
        self.btn_nav.clicked.connect(self.on_nav_mode)
        self.canvas.navRequested.connect(self.on_nav_target)
        self.btn_wall.setEnabled(False)
        self.btn_wall_mgr.setEnabled(False)
        self.btn_track.setEnabled(False)
        self.btn_track_mgr.setEnabled(False)
        self.btn_home.setEnabled(False)
        self.btn_reloc.setEnabled(False)
        self.btn_nav.setEnabled(False)
        self.btn_health.setEnabled(False)
        self._last_health_check = None   # 健康检查节流(秒)
        self._loc_quality_hint = ""      # 定位质量低提示(附加到状态栏)
        self._nav_active = False         # 点击导航进行中
        self._last_path_poll = None      # 剩余路径轮询节流(秒)

        # 星标交互接线
        self.panel.addModeRequested.connect(self.on_add_mode)
        self.panel.gotoRequested.connect(self.on_goto)
        self.panel.deleteRequested.connect(self.on_delete)
        self.panel.refreshRequested.connect(self.refresh_pois)
        self.panel.selectionChanged.connect(self.canvas.highlight_poi)
        self.panel.selectionChanged.connect(self.on_poi_selected)
        self.panel.headingDragRequested.connect(self.on_heading_drag)
        self.panel.headingValueRequested.connect(self.on_heading_value)
        self.canvas.placeRequested.connect(self.on_place)
        self.canvas.poiClicked.connect(self.panel.select_poi)

        # 遥控/调速接线
        self.control.moveTick.connect(self.on_move_tick)
        self.control.stopRequested.connect(self.on_move_stop)
        self.control.strategyChanged.connect(self.on_strategy)
        self.control.maxSpeedChanged.connect(self.on_max_speed)
        self.control.maxAngularChanged.connect(self.on_max_angular)
        self.control.estopRequested.connect(self.on_estop)
        self.control.brakeReleaseRequested.connect(self.on_brake_release)

        # 任务接线
        self.task_panel.newTask.connect(self.on_task_new)
        self.task_panel.editTask.connect(self.on_task_edit)
        self.task_panel.deleteTask.connect(self.on_task_delete)
        self.task_panel.startTask.connect(self.on_task_start)
        self.task_panel.pauseTask.connect(self.on_task_pause)
        self.task_panel.resumeTask.connect(self.on_task_resume)
        self.task_panel.abortTask.connect(self.on_task_abort)

        self.arm_panel.connectRequested.connect(self.on_arm_connect)
        self.arm_panel.disconnectRequested.connect(self.on_arm_disconnect)
        self.arm_panel.jointsChanged.connect(self.on_arm_joints)
        self.arm_panel.streamToggled.connect(self.arm_worker.set_streaming)
        self.arm_panel.speedChanged.connect(self.on_arm_speed)
        self.arm_panel.speedLimitToggled.connect(self.on_arm_speed_limit)
        self.arm_panel.sequencesRequested.connect(self.on_arm_sequences)
        self.arm_panel.brakeReleaseRequested.connect(self.on_arm_brake_stub)
        self.vision_panel.openRequested.connect(self.on_camera_open)
        self.vision_panel.closeRequested.connect(self.on_camera_close)
        self.vision_panel.snapshotRequested.connect(self.on_camera_snapshot)
        self.diagnosis_panel.btn_refresh.clicked.connect(self.refresh_diagnosis)
        self.mission_panel.runRequested.connect(self.on_mission_run)
        self.mission_panel.abortRequested.connect(self.on_mission_abort)
        self.mission_panel.refreshRequested.connect(self.refresh_missions)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        # 组1: 连接(状态灯 + IP/端口 + 连接)
        bar.addWidget(self.status_light)
        bar.addWidget(self.conn_label)
        bar.addWidget(QLabel("IP"))
        bar.addWidget(self.ip_edit)
        bar.addWidget(QLabel("端口"))
        bar.addWidget(self.port_edit)
        bar.addWidget(self.btn_connect)
        bar.addWidget(self._vline())
        # 组2: 地图
        bar.addWidget(self.btn_load)
        bar.addWidget(self.btn_pull)
        bar.addWidget(self.btn_fit)
        bar.addWidget(self.btn_overview)
        bar.addWidget(self.btn_max_map)
        bar.addWidget(self._vline())
        # 组3: 标注/动作
        bar.addWidget(self.btn_wall)
        bar.addWidget(self.btn_wall_mgr)
        bar.addWidget(self.btn_track)
        bar.addWidget(self.btn_track_mgr)
        bar.addWidget(self.btn_home)
        bar.addWidget(self.btn_reloc)
        bar.addWidget(self.btn_nav)
        bar.addWidget(self.btn_health)
        bar.addStretch(1)
        # 组4: 视图开关
        bar.addWidget(self.chk_track_first)
        bar.addWidget(self.chk_laser)
        bar.addWidget(self.chk_follow)

        # 主区分屏（地图 + 底盘遥控/机械臂/视觉）+ 右侧精简 Tab
        self.workspace = MainWorkspace()
        self.workspace.set_panels(
            map_widget=self.canvas,
            chassis=self.control,
            arm=self.arm_panel,
            vision=self.vision_panel,
        )

        self.tabs = QTabWidget()
        self.tabs.setFixedWidth(240)
        self.tabs.addTab(self.panel, "星标")
        self.tabs.addTab(self.task_panel, "任务")
        self.tabs.addTab(self.diagnosis_panel, "诊断")
        self.tabs.addTab(self.mission_panel, "任务组")

        body = QHBoxLayout()
        body.addWidget(self.workspace, 1)
        body.addWidget(self.tabs)

        root = QVBoxLayout()
        root.addLayout(bar)
        root.addLayout(body, 1)
        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)

        self.setStatusBar(QStatusBar())

        # 位姿轮询定时器 (~5Hz)
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.poll)

        # 启动时尝试载入本地地图
        if map_file:
            try:
                self.load_map(parse_stcm_file(map_file))
                self.status(f"已载入本地地图 {map_file}")
            except OSError:
                self.status("未找到默认地图, 请手动载入或从底盘拉取")

        # 载入已存任务(离线也显示, 仅按钮置灰)
        self.refresh_tasks()
        self.refresh_missions()
        self.refresh_diagnosis()

        self.cam_timer = QTimer(self)
        self.cam_timer.setInterval(100)
        self.cam_timer.timeout.connect(self.poll_camera)

        if host:
            self.on_connect()


    # ---- 整合：机械臂 / 视觉 / 诊断 / Mission ----

    def _on_arm_joints_from_worker(self, degs) -> None:
        QTimer.singleShot(0, lambda: self.arm_panel.set_joints_deg(degs))

    def on_arm_connect(self) -> None:
        ok = self.arm.connect()
        detail = self.arm.last_connect_error()
        self.arm_panel.set_connected(ok, detail)
        if ok:
            # 连接后立刻用 RTSI/种子角同步滑块与规划器，避免从 0° 猛跳超速
            j = self.arm.read_joints_deg()
            if j is not None:
                self.arm_panel.set_joints_deg(list(j))
                self.arm_worker.set_desired_deg(j)
            self.arm_worker.set_streaming(True)
            self.arm_panel.chk_stream.blockSignals(True)
            self.arm_panel.chk_stream.setChecked(True)
            self.arm_panel.chk_stream.blockSignals(False)
            self.status("机械臂已连接（已开流控；关节来自 RTSI/种子）")
        else:
            self.status("机械臂连接失败: " + detail)
        self.refresh_diagnosis()

    def on_arm_disconnect(self) -> None:
        self.arm_worker.set_streaming(False)
        self.arm_panel.chk_stream.blockSignals(True)
        self.arm_panel.chk_stream.setChecked(False)
        self.arm_panel.chk_stream.blockSignals(False)
        self.arm.disconnect()
        self.arm_panel.set_connected(False)
        self.status("机械臂已断开")
        self.refresh_diagnosis()

    def on_arm_joints(self, degs: list) -> None:
        self.arm_worker.set_desired_deg(degs)
        # 未开流控时也连发几步，避免“拖了滑块但臂不动”
        if self.arm.is_connected() and not self.arm_worker.is_streaming():
            self.arm_worker.request_flush(8)

    def on_arm_speed(self, v: float) -> None:
        self.cfg.arm.max_joint_speed_deg_s = float(v)

    def on_arm_speed_limit(self, on: bool) -> None:
        self.cfg.arm.speed_limit_enabled = bool(on)

    def on_arm_sequences(self) -> None:
        dlg = ActionSequencesDialog(
            self.cfg.data_dir,
            get_current_joints=lambda: self.arm.read_joints_deg() or self.arm_panel.joint_values_deg(),
            run_pose=lambda deg: self.arm_worker.set_desired_deg(deg),
            parent=self,
        )
        dlg.exec_()

    def on_arm_brake_stub(self) -> None:
        ok = self.arm.brake_release()
        self.status("手动释放已请求" if ok else "手动释放接口未接通(预留)")

    def on_camera_open(self) -> None:
        if self.camera.open():
            self.cam_timer.start()
            self.status("摄像头已打开")
        else:
            self.status("摄像头打开失败")
        self.refresh_diagnosis()

    def on_camera_close(self) -> None:
        self.cam_timer.stop()
        self.camera.close()
        self.vision_panel.view.setText("无画面")
        self.status("摄像头已关闭")
        self.refresh_diagnosis()

    def on_camera_snapshot(self) -> None:
        frame = self.camera.read_bgr()
        if frame is None:
            self.status("无画面可抓拍")
            return
        from datetime import datetime as _dt
        stamp = _dt.now().strftime("%Y%m%d_%H%M%S")
        path = self.cfg.data_dir / "snapshots" / ("snap_" + stamp + ".jpg")
        try:
            save_snapshot(frame, path)
            self.status("已抓拍 " + str(path))
        except Exception as e:
            self.status("抓拍失败: " + str(e))

    def poll_camera(self) -> None:
        self.vision_panel.show_bgr(self.camera.read_bgr())

    def refresh_diagnosis(self) -> None:
        self.diagnosis_panel.set_statuses(self.diag.collect())

    def refresh_missions(self) -> None:
        self.mission_panel.set_missions(self.mission_store.list_missions())

    def on_mission_run(self, payload) -> None:
        if isinstance(payload, tuple) and payload and payload[0] == "__create__":
            m = payload[1]
            self.mission_store.save(m)
            self.refresh_missions()
            self.status("已创建 Mission: " + m.name)
            return
        m = payload
        if self.mission_exec.is_running():
            self.status("已有 Mission 在执行")
            return
        self.mission_exec.chassis = self.client
        self.mission_exec.arm = self.arm
        self.mission_exec.camera = self.camera
        self.mission_exec.start(m, self.mission_store)
        self.status("开始执行 Mission: " + m.name)

    def on_mission_abort(self) -> None:
        self.mission_exec.abort()
        self.status("已请求中断 Mission")

    def on_workspace_overview(self) -> None:
        self.workspace.restore_overview()
        self.status("已恢复分屏总览")

    def on_workspace_max_map(self) -> None:
        self.workspace.maximize_map()
        self.status("地图已放大")

    def closeEvent(self, event) -> None:
        try:
            self.workspace.close_all_floats()
            self.arm_worker.stop()
            self.camera.close()
            self.arm.disconnect()
        except Exception:
            pass
        super().closeEvent(event)

    # ---- 通用 ----

    def status(self, msg: str, pin_secs: float = 0.0) -> None:
        """在状态栏显示消息。pin_secs>0 时固定该消息若干秒, poll 不覆盖。"""
        import time
        self.statusBar().showMessage(msg)
        if pin_secs > 0:
            self._status_pinned_until = time.monotonic() + pin_secs

    def _vline(self) -> QFrame:
        """工具栏分组竖分隔线。"""
        ln = QFrame()
        ln.setFrameShape(QFrame.VLine)
        ln.setFixedHeight(22)
        return ln

    def _set_conn_light(self, connected: bool, name: str = "") -> None:
        """切换连接状态灯颜色与文字(绿=已连/灰=未连)。"""
        if connected:
            self.status_light.setStyleSheet("color: #3fb950; font-size: 15px;")
            self.conn_label.setText(name or "已连接")
            self.conn_label.setStyleSheet("color: #e6e9ef;")
        else:
            self.status_light.setStyleSheet("color: #5b6370; font-size: 15px;")
            self.conn_label.setText("未连接")
            self.conn_label.setStyleSheet("color: #9aa3b2;")

    def load_map(self, stcm) -> None:
        grid = stcm.grid_map()
        if grid is None:
            self.status("地图中无栅格层")
            return
        self.canvas.load_grid(grid)
        dock = stcm.home_dock_pose()
        if dock:
            self.canvas.set_dock(dock[0], dock[1])
        self.canvas.set_walls(stcm.walls())
        self.canvas.set_tracks(stcm.tracks())
        if self._pois:
            self.canvas.set_pois(self._pois)

    # ---- 按钮回调 ----

    def on_load_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 STCM 地图", "map", "STCM 地图 (*.stcm);;所有文件 (*)"
        )
        if path:
            try:
                self.load_map(parse_stcm_file(path))
                self.status(f"已载入 {path}")
            except (OSError, ValueError) as e:
                self.status(f"载入失败: {e}")

    def on_connect(self) -> None:
        host = self.ip_edit.text().strip()
        try:
            port = int(self.port_edit.text())
        except ValueError:
            self.status("端口号无效")
            return
        self._port = port
        self.client = HermesClient(host, port)
        # 应用当前"轨道优先"开关到新连接
        self.client.move_mode = (
            MOVE_MODE_TRACK_FIRST if self.chk_track_first.isChecked()
            else MOVE_MODE_FREE
        )
        try:
            info = self.client.ping()
            model = info.get('modelName', '')
            self.status(f"已连接 {host} — {model}")
            self._set_conn_light(True, f"已连接 {model}" if model else "已连接")
            self.panel.set_online(True)
            self.control.set_online(True)
            self.task_panel.set_online(True)
            self.btn_wall.setEnabled(True)
            self.btn_wall_mgr.setEnabled(True)
            self.btn_track.setEnabled(True)
            self.btn_track_mgr.setEnabled(True)
            self.btn_home.setEnabled(True)
            self.btn_reloc.setEnabled(True)
            self.btn_nav.setEnabled(True)
            self.btn_health.setEnabled(True)
            self.refresh_pois()
            self.load_speed_settings()
            self.timer.start()
            self.estop_bus.bind(chassis=self.client)
            self.diag.chassis = self.client
            self.mission_exec.chassis = self.client
            self.refresh_diagnosis()
        except HermesError as e:
            self.client = None
            self._set_conn_light(False)
            self.panel.set_online(False)
            self.control.set_online(False)
            self.status(f"连接失败: {e}")

    def load_speed_settings(self) -> None:
        """连接后填充策略列表与当前速度到遥控面板。"""
        if not self.client:
            return
        try:
            raw_strategies = self.client.get_strategies()
            # 策略列表可能是 list[dict] 或 list[str]，统一提取 id 字段
            strategy_ids = []
            for s in raw_strategies:
                if isinstance(s, dict):
                    strategy_ids.append(s.get("id") or s.get("name") or str(s))
                else:
                    strategy_ids.append(str(s))
            current = self.client.get_current_strategy()
            # current 可能是 dict 或 str，提取 id
            if isinstance(current, dict):
                current_id = current.get("id") or current.get("name") or ""
            else:
                current_id = str(current)
            self.control.set_strategies(strategy_ids, current_id)
            self.control.set_speeds(
                self.client.get_max_speed(),
                self.client.get_max_angular_speed(),
            )
        except HermesError as e:
            self.status(f"读取速度设置失败: {e}")

    def on_pull_map(self) -> None:
        if not self.client:
            self.status("请先连接底盘")
            return
        try:
            data = self.client.get_map_stcm()
            self.load_map(parse_stcm(data))
            self.status(f"已从底盘拉取地图 ({len(data)} 字节)")
        except (HermesError, ValueError) as e:
            self.status(f"拉取失败: {e}")

    # ---- 星标 (功能表 #9 #10 #11) ----

    def refresh_pois(self) -> None:
        """重新拉取底盘星标, 同步到地图和列表。"""
        if not self.client:
            return
        try:
            self._pois = self.client.list_pois()
            self.canvas.set_pois(self._pois)
            self.panel.set_pois(self._pois)
            self.status(f"星标 {len(self._pois)} 个")
        except HermesError as e:
            self.status(f"读取星标失败: {e}")

    def on_add_mode(self) -> None:
        if not self.client:
            return
        self.canvas.set_place_mode(True)
        self.status("请在地图上点击要放置星标的位置")

    def on_place(self, x: float, y: float) -> None:
        """地图点击放置回调: 输名 -> add_poi -> 刷新。"""
        if not self.client:
            return
        name, ok = QInputDialog.getText(
            self, "添加星标", f"位置 ({x:.2f}, {y:.2f})\n请输入星标名称:"
        )
        if not ok or not name.strip():
            self.status("已取消添加星标")
            return
        try:
            self.client.add_poi(name.strip(), x, y)
            self.refresh_pois()
            self.status(f"已添加星标「{name.strip()}」")
        except HermesError as e:
            self.status(f"添加失败: {e}")

    def on_goto(self, poi_id: str) -> None:
        poi = self._find_poi(poi_id)
        if not poi or not self.client:
            return
        try:
            self.client.move_to_poi(poi)
            self._goto_name = poi.name
            self._goto_yaw = poi.yaw   # 到点后补转到星标朝向
            self.status(f"前往「{poi.name}」中…")
        except HermesError as e:
            self.status(f"调度失败: {e}")

    def on_delete(self, poi_id: str) -> None:
        poi = self._find_poi(poi_id)
        if not poi or not self.client:
            return
        if QMessageBox.question(
            self, "删除星标", f"确定删除「{poi.name}」?"
        ) != QMessageBox.Yes:
            return
        try:
            self.client.delete_poi(poi_id)
            self.refresh_pois()
            self.status(f"已删除「{poi.name}」")
        except HermesError as e:
            self.status(f"删除失败: {e}")

    def _find_poi(self, poi_id: str):
        for p in self._pois:
            if str(p.poi_id) == str(poi_id):
                return p
        return None

    # ---- 星标朝向编辑 (功能1) ----

    def on_poi_selected(self, poi_id: str) -> None:
        """选中星标时把其当前朝向同步到面板角度框。"""
        import math
        poi = self._find_poi(poi_id)
        if poi:
            self.panel.set_heading_value(math.degrees(poi.yaw or 0.0))

    def on_heading_drag(self, poi_id: str) -> None:
        """进入地图拖拽调朝向模式。"""
        poi = self._find_poi(poi_id)
        if not poi or not self.client:
            return
        self.canvas.set_poi_heading_mode(poi_id, poi.x, poi.y)
        self.status("在地图上从该星标拖拽指向朝向, 松开提交")

    def on_heading_value(self, poi_id: str, deg: float) -> None:
        """数值方式设朝向(度)。"""
        import math
        self.apply_poi_yaw_rad(poi_id, math.radians(deg))

    def apply_poi_yaw_rad(self, poi_id: str, yaw: float) -> None:
        """统一入口: 把某星标朝向改为 yaw(弧度), 调 update_poi 落库并刷新。"""
        poi = self._find_poi(poi_id)
        if not poi or not self.client:
            return
        import math
        try:
            self.client.update_poi(poi_id, poi.name, poi.x, poi.y, yaw)
        except HermesError as e:
            self.status(f"修改朝向失败: {e}")
            return
        except Exception as e:   # noqa: BLE001 兜底, 防止任何异常冒泡导致闪退
            self.status(f"修改朝向异常: {e}")
            return
        # 本地先更新该星标朝向并刷新(刷新失败也不崩)
        poi.yaw = yaw
        try:
            self.refresh_pois()
        except Exception:   # noqa: BLE001
            self.canvas.set_pois(self._pois)
        self.status(f"已设「{poi.name}」朝向 {math.degrees(yaw) % 360:.0f}°")

    # ---- 虚拟轨道 (功能2) ----

    def on_track_mode(self) -> None:
        if not self.client:
            return
        self.canvas.set_track_mode(True)
        self.status("画轨道: 左键依次落点, 右键或双击结束, Esc 取消")

    def on_track_drawn(self, points: list) -> None:
        if not self.client or len(points) < 2:
            return
        try:
            self.client.add_track(points)
            self.status(f"已添加轨道({len(points)} 点)")
            self._reload_tracks()
        except HermesError as e:
            self.status(f"添加轨道失败: {e}")

    def _reload_tracks(self) -> None:
        if not self.client:
            return
        try:
            # 用 REST /lines/tracks(实机确认可用), 返回 [{id,start,end}],
            # 转成 set_tracks 需要的 (id, x1, y1, x2, y2) 元组。
            segs = []
            for t in self.client.list_tracks():
                s, e = t.get("start", {}), t.get("end", {})
                segs.append((
                    t.get("id"),
                    s.get("x", 0.0), s.get("y", 0.0),
                    e.get("x", 0.0), e.get("y", 0.0),
                ))
            self.canvas.set_tracks(segs)
        except HermesError as e:
            self.status(f"刷新轨道失败: {e}")

    def on_manage_tracks(self) -> None:
        """管理虚拟轨道: 列出所有轨道线段, 选中删除。"""
        if not self.client:
            return
        try:
            tracks = self.client.list_tracks()
        except HermesError as e:
            self.status(f"读取轨道失败: {e}")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("管理虚拟轨道")
        dlg.resize(360, 320)
        lst = QListWidget()
        for w in tracks:
            s, e = w.get("start", {}), w.get("end", {})
            it = QListWidgetItem(
                f"#{w.get('id')}  "
                f"({s.get('x', 0):.2f},{s.get('y', 0):.2f})→"
                f"({e.get('x', 0):.2f},{e.get('y', 0):.2f})"
            )
            it.setData(256, w.get("id"))
            lst.addItem(it)
        btn_del = QPushButton("删除选中")
        btn_close = QPushButton("关闭")

        def do_del():
            it = lst.currentItem()
            if it is None:
                return
            tid = it.data(256)
            try:
                self.client.delete_track(tid)
                lst.takeItem(lst.row(it))
                self._reload_tracks()
                self.status(f"已删除轨道 #{tid}")
            except HermesError as e:
                self.status(f"删除失败: {e}")

        btn_del.clicked.connect(do_del)
        btn_close.clicked.connect(dlg.accept)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(f"共 {len(tracks)} 条轨道线段"))
        v.addWidget(lst, 1)
        row = QHBoxLayout()
        row.addWidget(btn_del)
        row.addWidget(btn_close)
        v.addLayout(row)
        dlg.exec_()

    # ---- 遥控 / 调速 (功能表 #5 #6) ----

    def on_move_tick(self, direction: int) -> None:
        """遥控连发: 周期收到方向 -> move_by（带 UI 速度比例）。"""
        if not self.client:
            return
        # 急停激活时拒绝发送运动指令, 并停止连发定时器
        if self.estop_bus.latched:
            self.control._stop()
            return
        try:
            ratio = self.control.teleop_speed_ratio(direction)
            self.client.move_by(direction, speed_ratio=ratio)
        except HermesError as e:
            self.control._stop()
            self.status(f"遥控失败: {e}")

    def on_move_stop(self) -> None:
        if not self.client:
            return
        try:
            self.client.cancel_current_action()
        except HermesError:
            pass  # 已无 action 时忽略

    def on_strategy(self, name: str) -> None:
        if not self.client:
            return
        try:
            self.client.set_strategy(name)
            self.status(f"运动策略切换为 {name}")
            # 策略可能改变速度上限, 回读刷新滑块
            self.control.set_speeds(
                self.client.get_max_speed(),
                self.client.get_max_angular_speed(),
            )
        except HermesError as e:
            self.status(f"切换策略失败: {e}")

    def on_max_speed(self, speed: float) -> None:
        if not self.client:
            return
        try:
            self.client.set_max_speed(speed)
            got = self.client.get_max_speed()
            self.control.set_speeds(got, self.client.get_max_angular_speed())
            self.status(f"最大线速度已设为 {got:.2f} m/s（读回确认）", pin_secs=4.0)
        except HermesError as e:
            self.status(f"线速度设置失败: {e}", pin_secs=4.0)

    def on_max_angular(self, speed: float) -> None:
        if not self.client:
            return
        try:
            self.client.set_max_angular_speed(speed)
            got = self.client.get_max_angular_speed()
            self.control.set_speeds(self.client.get_max_speed(), got)
            self.status(f"最大角速度已设为 {got:.2f} rad/s（读回确认）", pin_secs=4.0)
        except HermesError as e:
            self.status(f"角速度设置失败: {e}", pin_secs=4.0)

    # ---- 安全: 急停 / 刹车释放 ----

    def on_estop(self, trigger: bool) -> None:
        """系统急停：底盘 + 机械臂 + Mission（EStopBus）。"""
        if trigger:
            if QMessageBox.question(
                self, "确认急停",
                "确定触发系统急停? 将停止底盘运动、机械臂下发与任务组。"
            ) != QMessageBox.Yes:
                self.control.set_estop_state(False)
                return
            self.estop_bus.trigger()
            if self.client:
                try:
                    self.executor.abort(self.client)
                except Exception:
                    pass
            self.control.set_estop_state(True)
            self.status("已触发系统急停", pin_secs=8.0)
            return
        self.estop_bus.release()
        self.control.set_estop_state(False)
        self.status("已解除软件急停", pin_secs=4.0)

    def on_brake_release(self, release: bool) -> None:
        """刹车释放(可手推)/恢复制动。"""
        if not self.client:
            return
        try:
            self.client.set_brake_release(release)
            self.status("已释放刹车, 可手推底盘" if release else "已恢复刹车制动")
        except HermesError as e:
            self.status(f"刹车操作失败: {e}")

    # ---- 虚拟墙 (功能表 #7) ----

    def on_wall_mode(self) -> None:
        if not self.client:
            self.status("请先连接底盘")
            return
        self.canvas.set_wall_mode(True)
        self.status("在地图上按住拖拽画一条虚拟墙")

    def on_wall_drawn(self, x1: float, y1: float, x2: float, y2: float) -> None:
        if not self.client:
            return
        try:
            self.client.add_wall(x1, y1, x2, y2)
            self.status(
                f"已添加虚拟墙 ({x1:.2f},{y1:.2f})->({x2:.2f},{y2:.2f})"
            )
            self._reload_walls()
        except HermesError as e:
            self.status(f"添加虚拟墙失败: {e}")

    def _reload_walls(self) -> None:
        """从底盘重新拉地图, 刷新虚拟墙渲染。"""
        if not self.client:
            return
        try:
            data = self.client.get_map_stcm()
            self.canvas.set_walls(parse_stcm(data).walls())
        except (HermesError, ValueError) as e:
            self.status(f"刷新虚拟墙失败: {e}")

    def on_manage_walls(self) -> None:
        """管理虚拟墙: 列出所有墙, 选中删除(补足功能 拆除虚拟墙)。"""
        if not self.client:
            return
        try:
            walls = self.client.list_walls()
        except HermesError as e:
            self.status(f"读取虚拟墙失败: {e}")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("管理虚拟墙")
        dlg.resize(360, 320)
        lst = QListWidget()
        for w in walls:
            s, e = w.get("start", {}), w.get("end", {})
            it = QListWidgetItem(
                f"#{w.get('id')}  "
                f"({s.get('x', 0):.2f},{s.get('y', 0):.2f})→"
                f"({e.get('x', 0):.2f},{e.get('y', 0):.2f})"
            )
            it.setData(256, w.get("id"))
            lst.addItem(it)
        btn_del = QPushButton("删除选中")
        btn_close = QPushButton("关闭")

        def do_del():
            it = lst.currentItem()
            if it is None:
                return
            wid = it.data(256)
            try:
                self.client.delete_wall(wid)
                lst.takeItem(lst.row(it))
                self._reload_walls()
                self.status(f"已删除虚拟墙 #{wid}")
            except HermesError as e:
                self.status(f"删除失败: {e}")

        btn_del.clicked.connect(do_del)
        btn_close.clicked.connect(dlg.accept)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(f"共 {len(walls)} 条虚拟墙"))
        v.addWidget(lst, 1)
        row = QHBoxLayout()
        row.addWidget(btn_del)
        row.addWidget(btn_close)
        v.addLayout(row)
        dlg.exec_()

    def on_go_home(self) -> None:
        """回充电桩(补足功能)。"""
        if not self.client:
            return
        try:
            self.client.go_home()
            self._goto_name = "充电桩"
            self._goto_yaw = None
            self.status("正在返回充电桩…")
        except HermesError as e:
            self.status(f"回桩失败: {e}")

    # ---- 手动重定位 (治本定位质量低) ----

    def on_reloc_mode(self) -> None:
        if not self.client:
            return
        self.canvas.set_reloc_mode(True)
        self.status("重定位: 在地图上按住机器人实际位置, 拖拽指向其朝向, 松开提交")

    def on_reloc_done(self, x: float, y: float, yaw: float) -> None:
        if not self.client:
            return
        import math
        deg = math.degrees(yaw)
        if QMessageBox.question(
            self, "确认重定位",
            f"将机器人位姿强制设为:\n位置 ({x:.2f}, {y:.2f})  朝向 {deg:.0f}°\n"
            f"确认机器人当前确实在此位置?"
        ) != QMessageBox.Yes:
            self.status("已取消重定位")
            return
        try:
            self.client.set_pose(x, y, yaw)
            self.status(f"已重定位到 ({x:.2f}, {y:.2f}, {deg:.0f}°), 观察定位质量是否回升")
            # 触发一次健康刷新看质量变化
            self._last_health_check = None
            self._poll_health()
        except HermesError as e:
            self.status(f"重定位失败: {e}")

    # ---- 点击地图导航 (显示规划路线 + 自动出发) ----

    def on_track_first_toggle(self, on: bool) -> None:
        """切换轨道优先导航。影响后续所有 move_to(点击导航/前往/任务)。"""
        if self.client:
            self.client.move_mode = (
                MOVE_MODE_TRACK_FIRST if on else MOVE_MODE_FREE
            )
        self.status("已开启轨道优先导航(沿虚拟轨道)" if on
                    else "已切回自由导航")

    def on_nav_mode(self) -> None:
        if not self.client:
            return
        self.canvas.set_nav_mode(True)
        self.status("导航: 点击地图任意位置, 自动规划并前往")

    def on_nav_target(self, x: float, y: float) -> None:
        if not self.client:
            return
        try:
            pts = self.client.search_path(x, y)
        except HermesError as e:
            self.status(f"路径规划失败: {e}")
            return
        if not pts:
            self.canvas.clear_path()
            self.status("该点无法规划路径(障碍/未探索区/不可达)")
            return
        self.canvas.set_path(pts)
        try:
            self.client.move_to(x, y)
            self._nav_active = True
            self._goto_name = "目标点"
            self._goto_yaw = None
            self.status(f"已规划 {len(pts)} 点, 前往 ({x:.2f}, {y:.2f})…")
        except HermesError as e:
            self.status(f"出发失败: {e}")

    # ---- 底盘健康监控 (对标 RoboStudio) ----

    LOC_QUALITY_WARN = 50   # 定位质量低于此值给提示
    def _set_health_button(self, n_err: int, n_warn: int) -> None:
        """按健康状态给健康按钮上色与角标。"""
        if n_err:
            self.btn_health.setText(f"⚠ 健康({n_err})")
            self.btn_health.setStyleSheet("color:#ff9b9b; border-color:#e05555;")
        elif n_warn:
            self.btn_health.setText(f"⚠ 健康({n_warn})")
            self.btn_health.setStyleSheet("color:#e0c055; border-color:#c0a040;")
        else:
            self.btn_health.setText("⚠ 健康")
            self.btn_health.setStyleSheet("")

    def _poll_health(self) -> None:
        """每分钟拉一次健康+定位质量(节流), 更新健康按钮颜色与状态栏提示。"""
        if not self.client:
            return
        import time
        now = int(time.monotonic())
        # 每 2 秒一次
        if self._last_health_check is not None and now - self._last_health_check < 2:
            return
        self._last_health_check = now
        try:
            h = self.client.get_health_items()
        except HermesError:
            return
        errs = h.get("errors", [])
        n_err = sum(1 for e in errs if e.get("level", 0) >= 2)
        n_warn = sum(1 for e in errs if e.get("level", 0) == 1)
        self._set_health_button(n_err, n_warn)
        # 同步急停按钮(物理急停或他处触发时, 界面也反映)
        # 软件急停已 latch 时不覆盖按钮状态, 避免物理急停解除后错误清除 UI
        hw_estop = h.get("emergency_stop", False)
        if not self.estop_bus.latched:
            self.control.set_estop_state(hw_estop)
        # 定位质量低提示
        try:
            q = self.client.get_localization_quality()
            if isinstance(q, int) and q < self.LOC_QUALITY_WARN:
                self._loc_quality_hint = f"  ⚠定位质量低({q})"
            else:
                self._loc_quality_hint = ""
        except HermesError:
            self._loc_quality_hint = ""

    def on_show_health(self) -> None:
        """健康信息弹窗: 列出报警, 可逐条/全部清除(对标 RoboStudio)。"""
        if not self.client:
            return
        try:
            h = self.client.get_health_items()
            quality = self.client.get_localization_quality()
        except HermesError as e:
            self.status(f"读取健康信息失败: {e}")
            return
        levels = {1: "警告", 2: "错误", 3: "致命"}
        dlg = QDialog(self)
        dlg.setWindowTitle("底盘健康信息")
        dlg.resize(420, 320)
        lst = QListWidget()
        for e in h.get("errors", []):
            lvl = levels.get(e.get("level", 0), "?")
            it = QListWidgetItem(
                f"[{lvl}] {e.get('message', '')}  (code {e.get('code')})"
            )
            it.setData(256, e.get("code"))
            lst.addItem(it)

        def do_clear_one():
            it = lst.currentItem()
            if it is None:
                return
            code = it.data(256)
            try:
                self.client.clear_health(code)
                lst.takeItem(lst.row(it))
                self.status(f"已清除报警 code {code}")
            except HermesError as e:
                self.status(f"清除失败: {e}")

        def do_clear_all():
            for i in range(lst.count()):
                code = lst.item(i).data(256)
                try:
                    self.client.clear_health(code)
                except HermesError:
                    pass
            lst.clear()
            self.status("已清除全部报警")

        btn_one = QPushButton("移除选中")
        btn_all = QPushButton("全部清除")
        btn_close = QPushButton("关闭")
        btn_one.clicked.connect(do_clear_one)
        btn_all.clicked.connect(do_clear_all)
        btn_close.clicked.connect(dlg.accept)

        v = QVBoxLayout(dlg)
        qlabel = QLabel(f"定位质量: {quality} / 100")
        if isinstance(quality, int) and quality < self.LOC_QUALITY_WARN:
            qlabel.setStyleSheet("color:#ff9b9b; font-weight:bold;")
        v.addWidget(qlabel)
        v.addWidget(QLabel(f"共 {len(h.get('errors', []))} 条报警"))
        v.addWidget(lst, 1)
        tip = QLabel("提示: 清除仅消除记录; 若定位质量低, 底盘会重新报。"
                     "治本请回充电桩, 或点工具栏「重定位」手动校正位置。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#9aa3b2;")
        v.addWidget(tip)
        row = QHBoxLayout()
        row.addWidget(btn_one)
        row.addWidget(btn_all)
        row.addStretch(1)
        row.addWidget(btn_close)
        v.addLayout(row)
        dlg.exec_()
        # 关窗后刷新按钮状态
        self._last_health_check = None
        self._poll_health()

    # ---- 雷达点云 (实时渲染) ----

    def on_laser_toggle(self, on: bool) -> None:
        if not on:
            self.canvas.clear_laser()

    def _poll_laser(self) -> None:
        """轮询激光帧并渲染(仅勾选'显示雷达'时)。异常静默, 不打断主轮询。"""
        if not (self.client and self.chk_laser.isChecked()):
            return
        try:
            scan = self.client.get_laser_scan()
            self.canvas.set_laser_points(scan.world_points())
        except HermesError:
            pass

    # ---- 任务编排 (功能表 #12 #13 #14) ----

    def refresh_tasks(self) -> None:
        self.task_panel.set_tasks(self.store.list_tasks())

    def on_task_change(self, task) -> None:
        """执行器状态变化回调: 刷新列表 + 高亮当前航点。"""
        self.refresh_tasks()
        tid = self.executor.current_target_poi_id()
        if tid:
            self.canvas.highlight_poi(tid)
        if task.status == TaskStatus.DONE:
            self.status(f"任务「{task.name}」已完成")
        elif task.status == TaskStatus.ABORTED:
            self.status(f"任务「{task.name}」中断: {task.reason}")

    def on_task_new(self) -> None:
        dlg = TaskDialog(self._pois, parent=self)
        if dlg.exec_() != QDialog.Accepted:
            return
        f = dlg.result_fields()
        if not f["poi_ids"]:
            self.status("任务至少需要一个航点")
            return
        self.store.add(Task(id=None, **f))
        self.refresh_tasks()
        self.status(f"已新建任务「{f['name']}」")

    def on_task_edit(self, task_id: int) -> None:
        task = self.store.get(task_id)
        if not task:
            return
        if task.status == TaskStatus.RUNNING:
            self.status("任务执行中, 不能编辑")
            return
        dlg = TaskDialog(self._pois, task=task, parent=self)
        if dlg.exec_() != QDialog.Accepted:
            return
        f = dlg.result_fields()
        task.name = f["name"]
        task.poi_ids = f["poi_ids"]
        task.dwells = f["dwells"]
        task.schedule_kind = f["schedule_kind"]
        task.schedule_time = f["schedule_time"]
        task.cur_idx = 0
        task.status = TaskStatus.PENDING
        self.store.update(task)
        self.refresh_tasks()

    def on_task_delete(self, task_id: int) -> None:
        task = self.store.get(task_id)
        if not task:
            return
        if QMessageBox.question(
            self, "删除任务", f"确定删除「{task.name}」?"
        ) != QMessageBox.Yes:
            return
        self.store.delete(task_id)
        self.refresh_tasks()

    def on_task_start(self, task_id: int) -> None:
        if not self.client:
            return
        task = self.store.get(task_id)
        if task:
            self.executor.start(task, self.client)

    def on_task_pause(self) -> None:
        if self.client:
            self.executor.pause(self.client)

    def on_task_resume(self, task_id: int) -> None:
        if not self.client:
            return
        task = self.store.get(task_id)
        if task:
            self.executor.resume(task, self.client)

    def on_task_abort(self) -> None:
        if self.client:
            self.executor.abort(self.client)

    def _check_schedule(self) -> None:
        """每分钟检查定时任务(由 poll 节流调用)。"""
        now = datetime.now()
        minute = now.strftime("%Y-%m-%d %H:%M")
        if minute == self._last_sched_check:
            return
        self._last_sched_check = minute
        due = check_due(now, self.store.list_tasks())
        for tid in due:
            task = self.store.get(tid)
            if task is None:
                continue
            task.last_run_date = now.strftime("%Y-%m-%d")
            self.store.update(task)
            # 仅当无任务在跑时启动(严格串行); 否则本次跳过, 下个周期不再触发
            self.executor.start(task, self.client)
            self.status(f"定时触发任务「{task.name}」")
        if due:
            self.refresh_tasks()

    # ---- 定时轮询 (功能表 #2 #15) ----

    def poll(self) -> None:
        import time
        if not self.client:
            return
        try:
            pose = self.client.get_pose()
            self.canvas.update_robot(pose.x, pose.y, pose.yaw)
            p = self.client.get_power_status()
            base = (
                f"位姿 ({pose.x:.2f}, {pose.y:.2f}, {pose.yaw:.2f})  "
                f"电量 {p.battery_percentage}%  "
                f"{'充电中' if p.is_charging else p.docking_status}"
            )
            # 重要消息固定期间(急停/速度读回)不覆盖状态栏
            if time.monotonic() >= self._status_pinned_until:
                self.status(base + self._action_suffix() + self._loc_quality_hint)
        except HermesError as e:
            self.status(f"轮询异常: {e}")
        # 健康监控(节流到~2s) + 雷达点云(勾选时)
        self._poll_health()
        self._poll_laser()
        self._poll_nav_path()
        # 任务执行器心跳 + 定时检查(异常已在内部各自吞掉, 不影响轮询)
        self.executor.tick(self.client)
        self._check_schedule()

    def _poll_nav_path(self) -> None:
        """点击导航进行中: 节流刷新剩余路线; 到达/结束则清除。"""
        if not (self.client and self._nav_active):
            return
        import time
        now = time.monotonic()
        if self._last_path_poll is not None and now - self._last_path_poll < 0.4:
            return
        self._last_path_poll = now
        # action 结束(到达) -> 清路线收尾
        if self.client.get_current_action() is None:
            self._nav_active = False
            self.canvas.clear_path()
            return
        try:
            self.canvas.set_path(self.client.get_remaining_path())
        except HermesError:
            pass

    def _action_suffix(self) -> str:
        """当前运动任务状态文字, 用于状态栏(功能表 #15 指令显示)。"""
        if not self._goto_name:
            return ""
        act = self.client.get_current_action()
        if not act:
            name, self._goto_name = self._goto_name, ""
            # 到点后补转到目标朝向(MoveToAction 不保证终点朝向)
            if self._goto_yaw is not None:
                yaw, self._goto_yaw = self._goto_yaw, None
                try:
                    self.client.rotate_to(yaw)
                except HermesError:
                    pass
            return f"  | 「{name}」已结束"
        # action 状态结构因固件而异, 尽力提取 stage/status 文字
        stage = ""
        if isinstance(act, dict):
            st = act.get("state") or {}
            stage = st.get("status") if isinstance(st, dict) else ""
        return f"  | 前往「{self._goto_name}」{stage}"


def _default_map_file() -> str:
    """默认地图路径。打包(PyInstaller onefile)后资源在 sys._MEIPASS 下。"""
    import os
    base = getattr(sys, "_MEIPASS", os.path.abspath("."))
    bundled = os.path.join(base, "map", "202Lab.stcm")
    return bundled if os.path.exists(bundled) else "map/202Lab.stcm"


def _install_crash_logger():
    """全局异常钩子: 把崩溃写到 ~/.robot_controller/crash.log 并弹窗显示,
    解决打包 exe 无控制台、闪退看不到原因的问题。"""
    import os
    import traceback
    log_dir = os.path.join(os.path.expanduser("~"), ".robot_controller")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError:
        log_dir = "."
    log_path = os.path.join(log_dir, "crash.log")

    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("\n==== crash ====\n" + text + "\n")
        except OSError:
            pass
        try:
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.critical(None, "程序错误(已记录)",
                                 f"{exc_type.__name__}: {exc}\n\n"
                                 f"详细日志: {log_path}\n\n{text[-1500:]}")
        except Exception:
            pass
        # 不调用默认钩子(默认会终止), 尽量让程序继续

    sys.excepthook = hook


def main():
    from ui.connect_dialog import ConnectDialog
    from ui.theme import apply_theme

    _install_crash_logger()
    app = QApplication(sys.argv)
    apply_theme(app)

    cfg = load_devices_config()
    cfg.ensure_data_dir()

    host, port = cfg.chassis.host, cfg.chassis.port
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg in ("--offline", "offline"):
            host = ""
        elif ":" in arg:
            host, p = arg.rsplit(":", 1)
            port = int(p) if p.isdigit() else cfg.chassis.port
        else:
            host = arg

    if host:
        dlg = ConnectDialog(host=host, port=port)
        if dlg.exec_() == ConnectDialog.Accepted:
            host, port = dlg.host(), dlg.port()
        else:
            host = ""

    win = MainWindow(host=host, port=port, map_file=_default_map_file(), devices_cfg=cfg)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
