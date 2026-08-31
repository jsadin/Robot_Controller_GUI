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
import time
from datetime import datetime

# 必须在 import PyQt5 之前加载 elite_cs_sdk，否则 Windows 上会与 Qt 发生
# 原生库冲突（进程直接 0xC0000005 退出，点「连接」后进不了主界面）。
try:
    import elite_cs_sdk  # noqa: F401
except ImportError:
    pass

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
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


class _BootArmConnectThread(QThread):
    """启动时在后台连接机械臂，避免阻塞 UI 导致白屏。"""

    finished_ok = pyqtSignal(bool, str)

    def __init__(self, arm, parent=None):
        super().__init__(parent)
        self._arm = arm

    def run(self) -> None:
        try:
            ok = bool(self._arm.connect())
            detail = "" if ok else (self._arm.last_connect_error() or "失败")
            self.finished_ok.emit(ok, detail)
        except Exception as e:
            self.finished_ok.emit(False, str(e))


class _BootMapFetchThread(QThread):
    """后台拉取 STCM 地图字节，主线程再 load_map。"""

    finished_ok = pyqtSignal(object, str)  # bytes|None, error

    def __init__(self, client, parent=None):
        super().__init__(parent)
        self._client = client

    def run(self) -> None:
        try:
            data = self._client.get_map_stcm()
            self.finished_ok.emit(data, "")
        except Exception as e:
            self.finished_ok.emit(None, str(e))

from devices.chassis import (
    DIR_TURN_LEFT,
    DIR_TURN_RIGHT,
    HermesClient,
    HermesError,
)
from devices.chassis.client import MOVE_MODE_FREE, MOVE_MODE_TRACK_FIRST
from devices.chassis.stcm import parse_stcm, parse_stcm_file
from devices.config_loader import DevicesConfig, load_devices_config
from devices.arm import ArmController
from devices.camera import build_camera, save_snapshot, snapshot_path
from devices.camera.auto_zoom import AutoZoomController, ranging_too_far
from devices.ranging import build_ranging
from devices.common import EStopBus
from core import app_log
from core.config_pack import PackManager, default_pack_dir
from core.diagnosis import DiagnosisAggregator
from core.mission import MissionExecutor, MissionStatus, MissionStore, check_due_missions
from core.migrate_tasks import migrate_chassis_tasks_to_missions
from devices.arm.sequences import default_sequences_path, load_sequences
from ui.map_canvas import MapCanvas
from ui.poi_panel import PoiPanel
from ui.control_panel import ControlPanel
from ui.arm_panel import ArmPanel
from ui.arm_worker import ArmControlWorker
from ui.vision_panel import VisionPanel
from ui.diagnosis_panel import DiagnosisPanel
from ui.mission_panel import MissionPanel
from ui.action_sequences_dialog import ActionSequencesDialog
from ui.workspace import MainWorkspace


class MainWindow(QMainWindow):
    # 任务组执行线程 → 主线程（QueuedConnection，勿用跨线程 QTimer.singleShot）
    missionProgress = pyqtSignal(object)  # dict 快照
    missionStatusMsg = pyqtSignal(str)

    def __init__(
        self,
        host: str = "",
        port: int = 1448,
        map_file: str = "map/202Lab.stcm",
        devices_cfg: DevicesConfig | None = None,
        *,
        auto_connect: bool = True,
    ):
        super().__init__()
        self.setWindowTitle("机器人控制器 — 整合版")
        self.resize(1440, 900)
        self._port = port
        self._auto_connect = bool(auto_connect)
        self.cfg = devices_cfg or load_devices_config()
        self.cfg.ensure_data_dir()
        self.pack = PackManager(
            pack_dir=default_pack_dir(),
            data_dir=self.cfg.data_dir,
        )
        try:
            self.pack.ensure_layout(seed_from_home=True)
        except Exception as e:
            app_log.log_warn("pack", f"ensure_layout: {e}")

        self.missionProgress.connect(self._on_mission_progress_snap)
        self.missionStatusMsg.connect(self.status)

        self.client: HermesClient | None = None
        self.arm = ArmController(self.cfg)
        self.camera = build_camera(self.cfg)
        self.ranging = build_ranging(self.cfg)
        self.ranging.bind_arm(self.arm)
        self.estop_bus = EStopBus()
        self.estop_bus.bind(arm=self.arm, stop_mission=lambda: None)
        self.arm_worker = ArmControlWorker(self.arm)
        self.arm_worker.on_joints = self._on_arm_joints_from_worker
        # 静默启动时延后开启控制线程，避免与后台 connect 并发卡死/白屏
        self._arm_worker_started = False
        if not self._auto_connect:
            self.arm_worker.start()
            self._arm_worker_started = True
        self.mission_store = MissionStore(self.cfg.data_dir / "missions.db")
        self.mission_exec = MissionExecutor(
            chassis=None, arm=self.arm, camera=self.camera,
            data_dir=self.cfg.data_dir,
            on_status=self._emit_mission_status,
            on_progress=self._emit_mission_progress,
            arm_goto=self._mission_arm_goto,
            arm_at_target=self.arm.joint_at_target,
        )
        self.estop_bus.bind(stop_mission=self.mission_exec.abort)
        self.diag = DiagnosisAggregator(
            chassis=None,
            arm=self.arm,
            camera=self.camera,
            ranging=self.ranging,
            get_arm_streaming=lambda: self.arm_worker.is_streaming(),
        )

        self.canvas = MapCanvas()
        self.panel = PoiPanel()
        self.control = ControlPanel()
        self.arm_panel = ArmPanel()
        self.arm_panel.spin_speed.setValue(float(self.cfg.arm.max_joint_speed_deg_s))
        self.arm_panel.chk_limit.setChecked(bool(self.cfg.arm.speed_limit_enabled))
        self.vision_panel = VisionPanel()
        self.vision_panel.set_auto_zoom(bool(self.cfg.camera.auto_zoom))
        self._auto_zoom = AutoZoomController(near_m=float(self.cfg.camera.auto_zoom_near_m))
        self._auto_zoom.set_enabled(self.vision_panel.is_auto_zoom())
        self.diagnosis_panel = DiagnosisPanel()
        self.mission_panel = MissionPanel(
            get_pois=self._mission_poi_choices,
            get_sequences=self._mission_sequence_names,
            get_sequence_poses=self._mission_sequence_poses,
        )
        self._pois: list = []          # 当前星标缓存 (POI 对象)
        self._goto_name = ""           # 正在前往的星标名(状态栏显示用)
        self._goto_yaw = None          # 到点后需补转的目标朝向(弧度), None=不补转
        self._last_sched_check = None   # 上次调度检查的分钟, 防同分钟重复
        self._status_pinned_until = 0.0  # 重要状态消息的固定截止时间(monotonic)
        self._last_diag_alarm_key = None  # 诊断告警防抖
        self._arm_was_connected_at_estop = False  # 急停前臂是否已连（解除后重连）
        self._map_loaded = False
        self._map_loading = False  # 载入/拉取地图期间跳过 poll，避免 scene 并发崩溃

        # 顶部工具条
        self.status_light = QLabel("●")
        self.status_light.setToolTip("连接状态")
        self.conn_label = QLabel("未连接")
        self.ip_edit = QLineEdit(host or self.cfg.chassis.host or "192.168.11.1")
        self.ip_edit.setFixedWidth(120)
        self.port_edit = QLineEdit(str(port if port else self.cfg.chassis.port))
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
        self.btn_export_pack = QPushButton("⇪ 导出配置包")
        self.btn_export_pack.setToolTip(
            "导出现场配置包 ZIP（设备配置、动作组、任务组；已连接时含轨道/虚拟墙）"
        )
        self.btn_import_pack = QPushButton("⇩ 加载配置包")
        self.btn_import_pack.setToolTip(
            "从 ZIP/文件夹加载到 exe 旁 config/，可覆盖本地数据；已连接时同步墙/轨到机器人"
        )
        self.btn_fit = QPushButton("⛶ 适应窗口")
        self.btn_overview = QPushButton("总览")
        self.btn_overview.setObjectName("primary")
        self.btn_overview.setToolTip("恢复地图+底部分屏总览布局")
        self.btn_max_map = QPushButton("放大地图")
        self.btn_max_map.setToolTip("地图铺满主工作区")
        self.chk_follow = QCheckBox("跟随机器人")
        self.chk_laser = QCheckBox("显示雷达")
        self.chk_laser.setChecked(True)  # 默认启用激光显示
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
        self.btn_export_pack.clicked.connect(self.on_export_config_pack)
        self.btn_import_pack.clicked.connect(self.on_import_config_pack)
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
        self.canvas.modeCancelled.connect(self._on_map_mode_cancelled)
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
        self.vision_panel.openFolderRequested.connect(self.on_open_snapshot_folder)
        self.vision_panel.zoomStartRequested.connect(self.on_camera_zoom_start)
        self.vision_panel.ptzStopRequested.connect(self.on_camera_ptz_stop)
        self.vision_panel.autoZoomToggled.connect(self.on_auto_zoom_toggled)
        self._apply_ptz_caps()
        self.diagnosis_panel.btn_refresh.clicked.connect(self.refresh_diagnosis)
        self.diagnosis_panel.exportRequested.connect(self.on_export_logs)
        self.mission_panel.runRequested.connect(self.on_mission_run)
        self.mission_panel.pauseRequested.connect(self.on_mission_pause)
        self.mission_panel.resumeRequested.connect(self.on_mission_resume)
        self.mission_panel.abortRequested.connect(self.on_mission_abort)
        self.mission_panel.refreshRequested.connect(self.refresh_missions)
        self.mission_panel.saveRequested.connect(self.on_mission_save)
        self.mission_panel.deleteRequested.connect(self.on_mission_delete)

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
        bar.addWidget(self._vline())
        bar.addWidget(self.btn_export_pack)
        bar.addWidget(self.btn_import_pack)
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
        self.tabs.setFixedWidth(260)
        self.tabs.addTab(self.panel, "星标")
        self.tabs.addTab(self.mission_panel, "任务组")
        self.tabs.addTab(self.diagnosis_panel, "诊断")

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

        # 离线模式才预载本地地图；静默连接会从底盘拉取，避免本地+远端叠成「双地图」
        if map_file and not self._auto_connect:
            try:
                self.load_map(parse_stcm_file(map_file))
                self.status(f"已载入本地地图 {map_file}")
            except OSError:
                self.status("未找到默认地图, 请手动载入或从底盘拉取")

        # 旧巡检任务一次性导入任务组，再刷新列表
        self._migrated_tasks_n = migrate_chassis_tasks_to_missions(
            self.cfg.data_dir, self.mission_store
        )
        self.refresh_missions()
        self.refresh_diagnosis()
        if self._migrated_tasks_n:
            self.status(f"已导入 {self._migrated_tasks_n} 条旧巡检任务到任务组")

        self.cam_timer = QTimer(self)
        self.cam_timer.setInterval(33)
        self.cam_timer.timeout.connect(self.poll_camera)
        self._cam_shown_ts = None
        self._ptz_hold_timer = QTimer(self)
        self._ptz_hold_timer.setSingleShot(True)
        self._ptz_hold_timer.timeout.connect(self.on_camera_ptz_stop)
        self._auto_zoom_timer = QTimer(self)
        self._auto_zoom_timer.setInterval(500)
        self._auto_zoom_timer.timeout.connect(self._tick_auto_zoom)
        self._auto_zoom_manual = False

        # 静默连接须在首帧绘制之后分步执行；同步阻塞会导致白屏/假死
        self._boot_notes: list[str] = []
        self._boot_busy = False
        # 静默连接改由 mark_ui_ready() 在 show/首绘之后触发，避免构造期抢跑白屏
        if not self._auto_connect:
            self.timer.start()

    def mark_ui_ready(self) -> None:
        """主窗口已 show 并完成首绘后调用，再开始静默连接。"""
        if not self._auto_connect:
            return
        if self._boot_busy or self._boot_notes or self.diag.boot_notes:
            return
        self.status("界面已就绪，正在连接设备…")
        QTimer.singleShot(0, self.silent_boot_connect)

    # ---- 整合：机械臂 / 视觉 / 诊断 / Mission ----

    def silent_boot_connect(self) -> None:
        """启动静默初始化（分步，让出事件循环，避免白屏）。

        顺序：底盘 → 拉地图 → 机械臂 → 摄像头 → 诊断复检。
        """
        if self._boot_busy:
            return
        self._boot_busy = True
        self._boot_notes = []
        self.diag.chassis_last_error = None
        self.status("正在连接底盘…")
        QTimer.singleShot(0, self._boot_step_chassis)

    def _boot_step_chassis(self) -> None:
        try:
            self.on_connect()
            if self.client:
                self._boot_notes.append("底盘已连接")
            else:
                err = self.diag.chassis_last_error or "失败"
                self._boot_notes.append(f"底盘未连接（{err}）")
        except Exception as e:
            self.diag.chassis_last_error = str(e)
            self._boot_notes.append(f"底盘异常（{e}）")
        self.status("静默初始化：" + "；".join(self._boot_notes))
        QTimer.singleShot(0, self._boot_step_map)

    def _boot_step_map(self) -> None:
        if not self.client:
            self._boot_notes.append("地图跳过（底盘未连）")
            self.status("静默初始化：" + "；".join(self._boot_notes))
            QTimer.singleShot(0, self._boot_step_arm)
            return
        self.status("正在拉取地图…")
        th = _BootMapFetchThread(self.client, self)
        self._boot_map_thread = th
        th.finished_ok.connect(self._boot_on_map_done)
        th.start()

    def _boot_on_map_done(self, data, err: str) -> None:
        if data:
            try:
                # load_map 在线时会内部 _reload_tracks（REST 线路，不叠 STCM 轨）
                self.load_map(parse_stcm(data))
                self._boot_notes.append(f"地图已拉取（{len(data)} 字节）")
            except Exception as e:
                self._boot_notes.append(f"地图解析失败（{e}）")
                app_log.log_error("boot_map", str(e))
        else:
            self._boot_notes.append(f"地图拉取失败（{err or '未知'}）")
        self.status("静默初始化：" + "；".join(self._boot_notes))
        QTimer.singleShot(0, self._boot_step_arm)

    def _ensure_arm_worker(self) -> None:
        if not self._arm_worker_started:
            self.arm_worker.start()
            self._arm_worker_started = True

    def _boot_step_arm(self) -> None:
        self.status("正在连接机械臂…")
        th = _BootArmConnectThread(self.arm, self)
        self._boot_arm_thread = th
        th.finished_ok.connect(self._boot_on_arm_done)
        th.start()

    def _boot_on_arm_done(self, ok: bool, detail: str) -> None:
        self._ensure_arm_worker()
        if ok:
            self._boot_notes.append("机械臂已连接")
            self.arm_panel.set_connected(True, detail)
            try:
                j = self.arm.read_joints_deg()
                if j is not None:
                    self.arm_panel.set_joints_deg(list(j))
                    self.arm_worker.set_desired_deg(j)
            except Exception:
                pass
            self.arm_worker.set_streaming(True)
            self.arm_panel.chk_stream.blockSignals(True)
            self.arm_panel.chk_stream.setChecked(True)
            self.arm_panel.chk_stream.blockSignals(False)
        else:
            self.arm_panel.set_connected(False, detail)
            self._boot_notes.append(f"机械臂未连接（{detail or '失败'}）")
        self.refresh_diagnosis()
        self.status("静默初始化：" + "；".join(self._boot_notes))
        QTimer.singleShot(0, self._boot_step_camera)

    def _boot_step_camera(self) -> None:
        self._ensure_arm_worker()
        self.status("正在打开摄像头…")
        try:
            opened = bool(self.camera.open())
            if opened:
                self.cam_timer.start()
                self._apply_ptz_caps()
                self._sync_auto_zoom_timer()
                # 不阻塞等待首帧；由延时复检诊断刷新
                self._boot_notes.append("摄像头已打开（等待首帧）")
            else:
                getter = getattr(self.camera, "last_open_error", None)
                detail = getter() if callable(getter) else ""
                self._boot_notes.append(f"摄像头打开失败（{detail or '失败'}）")
        except Exception as e:
            self._boot_notes.append(f"摄像头异常（{e}）")
        QTimer.singleShot(0, self._boot_step_finish)

    def _boot_step_finish(self) -> None:
        self._ensure_arm_worker()
        self.diag.boot_notes = list(self._boot_notes)
        self.refresh_diagnosis()
        idx = self.tabs.indexOf(self.diagnosis_panel)
        if idx >= 0:
            self.tabs.setCurrentIndex(idx)
        self.status("静默初始化：" + "；".join(self._boot_notes))
        # 轮询在启动完成后开启（on_connect 成功时可能已 start，此处兜底）
        if not self.timer.isActive():
            self.timer.start()
        self._boot_busy = False
        app_log.log_info("boot", "；".join(self._boot_notes))
        # RTSP 首帧/臂状态可能滞后
        QTimer.singleShot(2500, self._refresh_boot_diagnosis)

    def _refresh_boot_diagnosis(self) -> None:
        """启动后复检：按实时状态改写启动摘要，修正「有画面却显示未连接」。"""
        notes: list[str] = []
        if self.client:
            notes.append("底盘已连接")
        else:
            err = self.diag.chassis_last_error or "未连接"
            notes.append(f"底盘未连接（{err}）")

        notes.append("地图已载入" if self._map_loaded else "地图未载入")

        if self.arm.is_connected():
            notes.append("机械臂已连接")
        else:
            notes.append("机械臂未连接（" + (self.arm.last_connect_error() or "失败") + "）")

        try:
            opened = bool(self.camera.is_open())
        except Exception:
            opened = False
        frame = None
        try:
            frame = self.camera.read_bgr()
        except Exception:
            frame = None
        if frame is not None:
            notes.append("摄像头已连接（有画面）")
        elif opened:
            notes.append("摄像头已打开（等待首帧）")
        else:
            notes.append("摄像头未连接")

        self.diag.boot_notes = notes
        self.refresh_diagnosis()

    def _on_arm_joints_from_worker(self, degs) -> None:
        QTimer.singleShot(0, lambda: self.arm_panel.set_joints_deg(degs))

    def on_arm_connect(self) -> None:
        self._ensure_arm_worker()
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
        dlg = getattr(self, "_arm_seq_dlg", None)
        if dlg is not None and dlg.is_runner_active():
            dlg.force_stop_runner(log_stop=True)
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

    def _run_arm_pose(self, deg) -> None:
        """动作组/位姿前往：写入期望角；未开流控时补发几步。"""
        self.arm_worker.set_desired_deg(deg)
        if self.arm.is_connected() and not self.arm_worker.is_streaming():
            self.arm_worker.request_flush(8)

    def on_arm_sequences(self) -> None:
        # 非模态 + 主窗口持有引用：关闭对话框不会中断已在跑的动作组
        dlg = getattr(self, "_arm_seq_dlg", None)
        if dlg is None:
            dlg = ActionSequencesDialog(
                self.cfg.data_dir,
                get_current_joints=lambda: self.arm.read_joints_deg()
                or self.arm_panel.joint_values_deg(),
                run_pose=self._run_arm_pose,
                is_arm_connected=self.arm.is_connected,
                on_status=self.status,
                parent=self,
            )
            self._arm_seq_dlg = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def on_arm_brake_stub(self) -> None:
        ok = self.arm.brake_release()
        self.status("手动释放已请求" if ok else "手动释放接口未接通(预留)")

    def on_camera_open(self) -> None:
        if self.camera.open():
            self.cam_timer.start()
            self._apply_ptz_caps()
            self._sync_auto_zoom_timer()
            self.status("摄像头已打开")
        else:
            err = getattr(self.camera, "last_open_error", None)
            detail = err() if callable(err) else (err or "")
            host = getattr(self.cfg.camera, "host", "")
            self.status(
                f"摄像头打开失败（{detail or host or '请检查 IP/密码'}）"
            )
        self.refresh_diagnosis()
        # 首帧晚到时再刷一次诊断
        QTimer.singleShot(2000, self.refresh_diagnosis)

    def on_camera_close(self) -> None:
        self.cam_timer.stop()
        self._auto_zoom_timer.stop()
        self._auto_zoom.reset()
        self._cam_shown_ts = None
        self.on_camera_ptz_stop()
        self.camera.close()
        self.vision_panel.view.setText("无画面")
        self.status("摄像头已关闭")
        self.refresh_diagnosis()

    def on_camera_snapshot(self) -> None:
        frame = None
        snap = getattr(self.camera, "snapshot_bgr", None)
        if callable(snap):
            frame = snap()
        if frame is None:
            frame = self.camera.read_bgr()
        if frame is None:
            self.status("无画面可抓拍")
            return
        from datetime import datetime as _dt
        stamp = _dt.now().strftime("%Y%m%d_%H%M%S")
        path = snapshot_path(self.cfg.data_dir, f"snap_{stamp}.jpg")
        try:
            save_snapshot(frame, path)
            self.status("已抓拍 " + str(path), pin_secs=6.0)
            app_log.log_info("snapshot", str(path))
        except Exception as e:
            self.status("抓拍失败: " + str(e), pin_secs=6.0)
            app_log.log_error("snapshot", str(e))

    def on_open_snapshot_folder(self) -> None:
        """打开当前 data_dir 下的抓拍根目录（按日分子目录）。"""
        from pathlib import Path
        import subprocess

        root = Path(self.cfg.data_dir) / "snapshots"
        try:
            root.mkdir(parents=True, exist_ok=True)
            # 优先打开今天子目录（若已有）
            today = root / __import__("datetime").datetime.now().strftime("%Y-%m-%d")
            target = today if today.is_dir() else root
            subprocess.Popen(["explorer", str(target)])
            self.status(f"抓拍目录: {target}", pin_secs=5.0)
        except Exception as e:
            self.status(f"打开抓拍目录失败: {e}", pin_secs=5.0)

    def _apply_ptz_caps(self) -> None:
        available = bool(getattr(self.camera, "ptz_available", lambda: False)())
        getter = getattr(self.camera, "ptz_caps", None)
        caps = getter() if callable(getter) else {}
        if not isinstance(caps, dict):
            caps = {}
        if caps.get("probed"):
            self.vision_panel.set_ptz_enabled(
                available,
                zoom=bool(caps.get("zoom", True)),
                detail=str(caps.get("detail") or ""),
            )
        else:
            self.vision_panel.set_ptz_enabled(available)

    def on_auto_zoom_toggled(self, on: bool) -> None:
        self._auto_zoom.set_enabled(bool(on))
        if not on:
            self._auto_zoom_timer.stop()
            self.on_camera_ptz_stop()
            self.status("已取消自动变焦")
            return
        self._sync_auto_zoom_timer()
        near_mm = float(self.cfg.camera.auto_zoom_near_m) * 1000.0
        self.status(f"自动变焦：>{near_mm:.0f}mm/过远→最长焦，≤{near_mm:.0f}mm→最短焦")

    def _sync_auto_zoom_timer(self) -> None:
        cam_open = bool(getattr(self.camera, "is_open", lambda: False)())
        ptz_ok = bool(getattr(self.camera, "ptz_available", lambda: False)())
        if self._auto_zoom.enabled and cam_open and ptz_ok:
            if not self._auto_zoom_timer.isActive():
                self._auto_zoom_timer.start()
            self._tick_auto_zoom()
        else:
            self._auto_zoom_timer.stop()

    def _tick_auto_zoom(self) -> None:
        if not self._auto_zoom.enabled:
            return
        if not bool(getattr(self.camera, "is_open", lambda: False)()):
            return
        if not bool(getattr(self.camera, "ptz_available", lambda: False)()):
            return
        dist = None
        getter = getattr(self.ranging, "get_distance_m", None)
        if callable(getter):
            try:
                dist = getter()
            except Exception:
                dist = None
        err = ""
        err_fn = getattr(self.ranging, "last_error", None)
        if callable(err_fn):
            err = err_fn() or ""
        elif isinstance(err_fn, str):
            err = err_fn
        action = self._auto_zoom.tick(dist, too_far=ranging_too_far(err))
        if action in ("start_max", "hold_max"):
            if action == "start_max":
                self._start_camera_zoom(1, travel_ms=int(self.cfg.camera.auto_zoom_travel_ms))
                self.status(self._auto_zoom_status("最长焦", dist, err))
            else:
                self._keep_camera_zoom(1)
        elif action in ("start_min", "hold_min"):
            if action == "start_min":
                self._start_camera_zoom(-1, travel_ms=int(self.cfg.camera.auto_zoom_travel_ms))
                self.status(self._auto_zoom_status("最短焦", dist, err))
            else:
                self._keep_camera_zoom(-1)

    def on_camera_zoom_start(self, direction: int) -> None:
        if self.vision_panel.is_auto_zoom():
            self.vision_panel.set_auto_zoom(False)
            self._auto_zoom.set_enabled(False)
            self._auto_zoom_timer.stop()
            self._auto_zoom.reset()
        self._auto_zoom_manual = True
        self._start_camera_zoom(int(direction), travel_ms=8000)

    def _auto_zoom_status(self, gear: str, dist: float | None, err: str) -> str:
        if dist is not None:
            return f"自动变焦→{gear}（{dist * 1000.0:.0f} mm）"
        extra = (err or "无有效距离").strip()
        return f"自动变焦→{gear}（{extra}）"

    def _keep_camera_zoom(self, direction: int) -> None:
        """行程中续发连续变倍，海康连续 PTZ 不续发容易自行停住。"""
        fn = getattr(self.camera, "zoom_start", None)
        if callable(fn):
            fn(int(direction))

    def _start_camera_zoom(self, direction: int, *, travel_ms: int) -> None:
        fn = getattr(self.camera, "zoom_start", None)
        if not callable(fn) or not fn(int(direction)):
            err = ""
            getter = getattr(self.camera, "ptz_last_error", None)
            if callable(getter):
                err = getter() or ""
            self.status(err or "当前相机不支持 ISAPI 变焦（需 hikvision/rtsp + 网页端口）")
            self._auto_zoom.reset()
            return
        ms = max(200, int(travel_ms))
        self._ptz_hold_timer.start(ms)
        if self._auto_zoom_manual:
            self.status("变焦+" if direction >= 0 else "变焦-")

    def on_camera_ptz_stop(self) -> None:
        self._ptz_hold_timer.stop()
        fn = getattr(self.camera, "ptz_stop", None)
        if callable(fn):
            fn()
        if self._auto_zoom.enabled and not self._auto_zoom_manual:
            self._auto_zoom.on_travel_done()
        self._auto_zoom_manual = False
        err = ""
        getter = getattr(self.camera, "ptz_last_error", None)
        if callable(getter):
            err = getter() or ""
        if err:
            self.status(f"变焦失败: {err}", pin_secs=6.0)
            app_log.log_warn("camera", err)

    def poll_camera(self) -> None:
        ts = getattr(self.camera, "last_frame_ts", None)
        ts = ts() if callable(ts) else ts
        if ts is not None and ts == getattr(self, "_cam_shown_ts", None):
            return
        self._cam_shown_ts = ts
        self.vision_panel.show_bgr(self.camera.read_bgr())

    def refresh_diagnosis(self) -> None:
        self.diagnosis_panel.set_boot_notes(getattr(self.diag, "boot_notes", []) or [])
        statuses, summary = self.diag.collect_with_summary()
        self.diagnosis_panel.set_statuses(statuses, summary)
        alarm_key = frozenset(
            (a.device.value, a.code, a.level.value)
            for s in statuses
            for a in (s.alarms or [])
        )
        if alarm_key != self._last_diag_alarm_key:
            prev = self._last_diag_alarm_key
            self._last_diag_alarm_key = alarm_key
            if prev is not None or alarm_key:
                if alarm_key:
                    app_log.log_warn(
                        "diagnosis",
                        f"告警变化 overall={summary.overall.value} "
                        f"faults={summary.fault_count} warns={summary.warn_count} "
                        f"codes={sorted(c for _, c, _ in alarm_key)}",
                    )
                else:
                    app_log.log_info("diagnosis", "告警已清除")

    def on_export_logs(self) -> None:
        """打包 app.log / crash.log / 诊断快照 / 状态环形缓冲。"""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"robot_logs_{stamp}.zip"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出日志", default_name, "Zip (*.zip)"
        )
        if not path:
            return
        try:
            out = app_log.export_bundle(path, self.diag.collect_snapshot())
            self.status(f"日志已导出: {out}", pin_secs=6.0)
            app_log.log_info("export", f"logs exported to {out}")
        except Exception as e:
            self.status(f"导出日志失败: {e}", pin_secs=6.0)
            app_log.log_error("export", str(e))

    def on_export_config_pack(self) -> None:
        """导出现场配置包 ZIP（可编辑目录 config/ 的快照）。"""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出配置包",
            f"robot_pack_{stamp}.zip",
            "配置包 Zip (*.zip)",
        )
        if not path:
            return
        try:
            notes: list[str] = []
            result = self.pack.export_zip(
                path,
                client=self.client,
                log=lambda m: notes.append(m),
            )
            detail = "；".join(result.messages[-4:] or notes[-4:] or ["完成"])
            self.status(f"配置包已导出: {path} — {detail}", pin_secs=8.0)
            app_log.log_info(
                "pack",
                f"export ok={result.ok} modules={result.modules_done} "
                f"skip={result.modules_skipped} -> {path}",
            )
        except Exception as e:
            self.status(f"导出配置包失败: {e}", pin_secs=6.0)
            app_log.log_error("pack", str(e))

    def on_import_config_pack(self) -> None:
        """从 ZIP 或文件夹加载到同目录 config/，并热重载任务/动作；在线则同步墙轨。"""
        path, filt = QFileDialog.getOpenFileName(
            self,
            "加载配置包（ZIP）",
            "",
            "配置包 Zip (*.zip);;所有文件 (*.*)",
        )
        src_dir = None
        if not path:
            src_dir = QFileDialog.getExistingDirectory(
                self, "或选择配置包文件夹（含 pack.json）"
            )
            if not src_dir:
                return
        if QMessageBox.question(
            self,
            "加载配置包",
            "将覆盖本机 config/ 中对应模块（任务组、动作组、设备配置等）。\n"
            "若已连接底盘，将按包内 JSON 重建虚拟墙与轨道。\n\n继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            if path:
                result = self.pack.import_zip(path, client=self.client)
            else:
                result = self.pack.load_from_dir(src_dir, client=self.client)
            self._after_pack_loaded(result)
        except Exception as e:
            self.status(f"加载配置包失败: {e}", pin_secs=6.0)
            app_log.log_error("pack", str(e))

    def _after_pack_loaded(self, result) -> None:
        """配置包落地后：重建任务库、刷地图标注；设备 yaml 提示重启。"""
        try:
            self.mission_store = MissionStore(self.cfg.data_dir / "missions.db")
            self.mission_exec.data_dir = self.cfg.data_dir
            self.refresh_missions()
        except Exception as e:
            app_log.log_warn("pack", f"reload missions: {e}")
        if self.client:
            try:
                self._reload_tracks()
            except Exception:
                pass
            try:
                self._reload_walls()
            except Exception:
                pass
            try:
                self.refresh_pois()
            except Exception:
                pass
        detail = "；".join((result.messages or [])[-5:])
        self.status(
            f"配置包已加载（模块 {','.join(result.modules_done) or '-'}）。"
            f"设备 IP/臂类型等若有变更请重启程序。{(' ' + detail) if detail else ''}",
            pin_secs=10.0,
        )
        app_log.log_info(
            "pack",
            f"import ok={result.ok} done={result.modules_done} "
            f"skip={result.modules_skipped}",
        )
        QMessageBox.information(
            self,
            "配置包",
            "加载完成。\n\n"
            f"已应用: {', '.join(result.modules_done) or '（无）'}\n"
            f"跳过: {', '.join(result.modules_skipped) or '（无）'}\n\n"
            "任务组/动作组已立即生效。\n"
            "若修改了 devices.local.yaml（IP/臂类型等），请重启程序。",
        )

    def _mission_poi_choices(self):
        out = []
        for p in getattr(self, "_pois", None) or []:
            out.append((str(p.poi_id), str(p.name)))
        return out

    def _mission_sequence_names(self):
        try:
            seqs = load_sequences(default_sequences_path(self.cfg.data_dir))
        except Exception:
            return []
        return sorted(seqs.keys())

    def _mission_sequence_poses(self, name: str):
        """动作组内有序位姿名（用于分步抓拍勾选）。"""
        try:
            seqs = load_sequences(default_sequences_path(self.cfg.data_dir))
        except Exception:
            return []
        entry = seqs.get(str(name or "").strip())
        if not entry:
            return []
        _loop, _count, steps = entry
        return [pn for pn, _d in steps]

    def refresh_missions(self) -> None:
        self.mission_panel.set_missions(self.mission_store.list_missions())

    def _emit_mission_status(self, msg: str) -> None:
        self.missionStatusMsg.emit(str(msg or ""))

    def _emit_mission_progress(self, m) -> None:
        """后台线程调用：只发快照，由主线程槽刷新 UI。"""
        try:
            total = len(m.steps) if m is not None else 0
            snap = {
                "id": getattr(m, "id", None),
                "name": getattr(m, "name", "") or "",
                "status": getattr(m, "status", "") or "",
                "cur_idx": int(getattr(m, "cur_idx", 0) or 0),
                "total": int(total),
                "reason": getattr(m, "reason", "") or "",
            }
        except Exception:
            return
        self.missionProgress.emit(snap)

    def _on_mission_progress_snap(self, snap) -> None:
        if not isinstance(snap, dict):
            return
        name = snap.get("name") or ""
        status = snap.get("status") or ""
        cur_idx = int(snap.get("cur_idx") or 0)
        total = int(snap.get("total") or 0)
        reason = snap.get("reason") or ""
        step_disp = MissionPanel.display_step_fraction(cur_idx, total, status)
        if status == MissionStatus.DONE:
            text = f"进度：{name} [已完成] {step_disp}"
        elif status == MissionStatus.ABORTED:
            text = f"进度：{name} [已中断] {step_disp}" + (f" — {reason}" if reason else "")
        elif status == MissionStatus.PAUSED:
            text = f"进度：{name} [已暂停] 步骤 {step_disp}" + (f" — {reason}" if reason else "")
        else:
            text = f"进度：{name} [{status}] 步骤 {step_disp}" + (f" — {reason}" if reason else "")
        self.mission_panel.set_progress_text(text)
        self.refresh_missions()

    def on_mission_save(self, m) -> None:
        self.mission_store.save(m)
        self.refresh_missions()
        self.status("已保存任务组: " + m.name)

    def on_mission_delete(self, mid: int) -> None:
        self.mission_store.delete(int(mid))
        self.refresh_missions()
        self.status(f"已删除任务组 #{mid}")

    def _mission_arm_goto(self, deg6) -> None:
        """任务组跑动作组：与「动作组」对话框同一条下发路径（经 ArmControlWorker）。"""
        self.arm_worker.set_desired_deg(deg6)
        if self.arm.is_connected() and not self.arm_worker.is_streaming():
            # 未开流控时临时打开，否则只会 flush 几步到不了位
            self.arm_worker.set_streaming(True)
            try:
                self.arm_panel.chk_stream.blockSignals(True)
                self.arm_panel.chk_stream.setChecked(True)
                self.arm_panel.chk_stream.blockSignals(False)
            except Exception:
                pass

    def _mission_bind_devices(self) -> None:
        self.mission_exec.chassis = self.client
        self.mission_exec.arm = self.arm
        self.mission_exec.camera = self.camera
        self.mission_exec.arm_goto = self._mission_arm_goto
        self.mission_exec.arm_at_target = self.arm.joint_at_target

    def _mission_reload(self, m):
        if m is None or m.id is None:
            return m
        return self.mission_store.get(int(m.id)) or m

    def on_mission_run(self, payload) -> None:
        m = self._mission_reload(payload)
        if self.mission_exec.is_running():
            self.status("已有任务组在执行")
            return
        if not m.steps:
            self.status("任务组无步骤，请先编辑")
            return
        self._mission_bind_devices()
        try:
            self.mission_exec.start(m, self.mission_store, resume=False)
        except RuntimeError as e:
            self.status(str(e))
            return
        self.mission_panel.set_progress_text(f"进度：{m.name} [执行中] 启动…")
        self.refresh_missions()
        self.status("开始执行任务组: " + m.name)

    def on_mission_pause(self) -> None:
        if not self.mission_exec.is_running():
            self.status("当前没有执行中的任务组")
            return
        self.mission_exec.pause()
        self.status("已请求暂停任务组")

    def on_mission_resume(self, payload) -> None:
        m = self._mission_reload(payload)
        if self.mission_exec.is_running():
            self.status("已有任务组在执行")
            return
        if m.status != MissionStatus.PAUSED:
            self.status("仅「已暂停」的任务组可恢复，其它请点「执行」")
            return
        self._mission_bind_devices()
        try:
            self.mission_exec.start(m, self.mission_store, resume=True)
        except RuntimeError as e:
            self.status(str(e))
            return
        self.status("恢复执行任务组: " + m.name)
        self.refresh_missions()

    def on_mission_abort(self) -> None:
        self.mission_exec.abort()
        self.refresh_missions()
        self.status("已请求中断任务组")

    def _check_mission_schedule(self) -> None:
        """每分钟检查任务组定时（由 poll 节流）。"""
        now = datetime.now()
        minute = now.strftime("%Y-%m-%d %H:%M")
        if minute == self._last_sched_check:
            return
        self._last_sched_check = minute
        due = check_due_missions(now, self.mission_store.list_missions())
        for mid in due:
            m = self.mission_store.get(mid)
            if m is None:
                continue
            if self.mission_exec.is_running():
                break
            m.last_run_date = now.strftime("%Y-%m-%d")
            self.mission_store.save(m)
            self._mission_bind_devices()
            try:
                self.mission_exec.start(m, self.mission_store, resume=False)
                self.status(f"定时触发任务组「{m.name}」")
            except RuntimeError as e:
                self.status(str(e))
        if due:
            self.refresh_missions()

    def on_workspace_overview(self) -> None:
        self.workspace.restore_overview()
        self.status("已恢复分屏总览")

    def on_workspace_max_map(self) -> None:
        self.workspace.maximize_map()
        self.status("地图已放大")

    def closeEvent(self, event) -> None:
        try:
            self._auto_zoom_timer.stop()
            self._ptz_hold_timer.stop()
            fn = getattr(self.camera, "ptz_stop", None)
            if callable(fn):
                fn()
            self.workspace.close_all_floats()
            self.arm_worker.stop()
            self.camera.close()
            self.ranging.close()
            self.arm.disconnect()
        except Exception:
            pass
        super().closeEvent(event)

    # ---- 通用 ----

    def status(self, msg: str, pin_secs: float = 0.0) -> None:
        """在状态栏显示消息。pin_secs>0 时固定该消息若干秒, poll 不覆盖。"""
        text = str(msg or "")
        self.statusBar().showMessage(text)
        if pin_secs > 0:
            self._status_pinned_until = time.monotonic() + pin_secs
        # 位姿/轮询刷屏不写日志；其余状态栏文案进环形缓冲
        if text and not text.startswith("位姿 (") and not text.startswith("轮询异常"):
            app_log.log_info("status", text)

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
        self._map_loading = True
        try:
            self.canvas.load_grid(grid)
            dock = stcm.home_dock_pose()
            if dock:
                self.canvas.set_dock(dock[0], dock[1])
            self.canvas.set_walls(stcm.walls())
            # 在线：轨道以 REST 为准（含线路名）；勿再画 STCM 轨道，否则与 _reload_tracks 重叠
            if self.client:
                self._reload_tracks()
            else:
                self.canvas.set_tracks(stcm.tracks())
            if self._pois:
                self.canvas.set_pois(self._pois)
            self._map_loaded = True
        finally:
            self._map_loading = False

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
            self.diag.chassis_last_error = "端口号无效"
            self.status("端口号无效")
            self.refresh_diagnosis()
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
            self.btn_wall.setEnabled(True)
            self.btn_wall_mgr.setEnabled(True)
            self.btn_track.setEnabled(True)
            self.btn_track_mgr.setEnabled(True)
            self.btn_home.setEnabled(True)
            self.btn_reloc.setEnabled(True)
            self.btn_nav.setEnabled(True)
            self.btn_health.setEnabled(True)
            self.refresh_pois()
            # 启动流程中延后刷轨道，避免与随后 load_map(scene.clear) 交错出问题
            if not getattr(self, "_boot_busy", False):
                self._reload_tracks()
            self.load_speed_settings()
            # 静默启动过程中先不轮询：雷达/诊断会占满主线程导致白屏
            if not getattr(self, "_boot_busy", False):
                self.timer.start()
            self.estop_bus.bind(chassis=self.client)
            self.diag.chassis = self.client
            self.diag.chassis_last_error = None
            self.mission_exec.chassis = self.client
            if not getattr(self, "_boot_busy", False):
                self.refresh_diagnosis()
        except HermesError as e:
            self.client = None
            self.diag.chassis = None
            self.diag.chassis_last_error = str(e)
            self._set_conn_light(False)
            self.panel.set_online(False)
            self.control.set_online(False)
            self.status(f"连接失败: {e}")
            self.refresh_diagnosis()

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
        if self._map_loading or getattr(self, "_boot_busy", False):
            self.status("地图正在加载，请稍候")
            return
        try:
            data = self.client.get_map_stcm()
            self.load_map(parse_stcm(data))
            # 星标不在 STCM 里，拉完地图后重刷，避免旧场景引用
            try:
                self.refresh_pois()
            except Exception:
                pass
            self.status(f"已从底盘拉取地图 ({len(data)} 字节)")
        except (HermesError, ValueError) as e:
            self.status(f"拉取失败: {e}")
        except Exception as e:
            self.status(f"拉取地图异常: {e}")
            app_log.log_error("pull_map", str(e))

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

    def _enter_map_mode(self, enable_fn, hint: str) -> None:
        """进入地图交互模式：先清其它模式，聚焦画布以便 Esc 退出。"""
        self.canvas.cancel_active_modes()
        enable_fn()
        self.canvas.setFocus(Qt.OtherFocusReason)
        self.status(hint)

    def _on_map_mode_cancelled(self) -> None:
        self.status("已退出地图操作模式（Esc）")

    def keyPressEvent(self, event) -> None:
        # 焦点不在画布时也能 Esc 退出地图模式
        if event.key() == Qt.Key_Escape and self.canvas.cancel_active_modes():
            self._on_map_mode_cancelled()
            return
        super().keyPressEvent(event)

    def on_add_mode(self) -> None:
        if not self.client:
            return
        self._enter_map_mode(
            lambda: self.canvas.set_place_mode(True),
            "放置星标: 点击地图选点，Esc 退出",
        )

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
        self._enter_map_mode(
            lambda: self.canvas.set_poi_heading_mode(poi_id, poi.x, poi.y),
            "拖拽星标朝向: 从星标拖出方向后松开；Esc 退出",
        )

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
        self._enter_map_mode(
            lambda: self.canvas.set_track_mode(True),
            "画轨道: 左键依次落点，右键或双击结束，Esc 退出",
        )

    def on_track_drawn(self, points: list) -> None:
        if not self.client or len(points) < 2:
            return
        n_seg = len(points) - 1
        name, ok = QInputDialog.getText(
            self,
            "轨道名称",
            f"本次绘制 {len(points)} 个点 / {n_seg} 段，将作为一条线路保存。\n"
            f"请输入线路名称:",
        )
        if not ok:
            self.status("已取消添加轨道")
            return
        name = (name or "").strip()
        if not name:
            self.status("轨道名称不能为空")
            return
        try:
            info = self.client.add_track(points, name=name)
            self.status(
                f"已添加线路「{info.get('name', name)}」"
                f"（{info.get('segments', n_seg)} 段）"
            )
            self._reload_tracks()
        except HermesError as e:
            self.status(f"添加轨道失败: {e}")

    def _reload_tracks(self) -> None:
        if not self.client:
            return
        try:
            # REST 线段按 route_id 聚合成线路，地图画线并标名称
            raw = self.client.list_tracks()
            routes = HermesClient.group_tracks_by_route(raw)
            self.canvas.set_track_routes(routes)
        except HermesError as e:
            self.status(f"刷新轨道失败: {e}")
        except Exception as e:
            # 勿让轨道渲染异常阻断连接/启动流程
            self.status(f"刷新轨道异常: {e}")
            app_log.log_error("tracks", str(e))

    def on_manage_tracks(self) -> None:
        """管理虚拟轨道: 按线路（一次绘制的多段）列出，删除整条线路。"""
        if not self.client:
            return
        try:
            raw = self.client.list_tracks()
        except HermesError as e:
            self.status(f"读取轨道失败: {e}")
            return
        routes = HermesClient.group_tracks_by_route(raw)
        dlg = QDialog(self)
        dlg.setWindowTitle("管理虚拟轨道")
        dlg.resize(420, 360)
        lst = QListWidget()
        for r in routes:
            segs = r.get("segments") or []
            ids = [s.get("id") for s in segs if isinstance(s, dict)]
            it = QListWidgetItem(
                f"{r.get('name') or '未命名'}  "
                f"（{len(segs)} 段）"
            )
            it.setData(256, ids)
            it.setData(257, r.get("route_id"))
            lst.addItem(it)
        btn_del = QPushButton("删除选中线路")
        btn_close = QPushButton("关闭")
        lbl = QLabel(f"共 {len(routes)} 条线路（{len(raw)} 段）")

        def do_del():
            it = lst.currentItem()
            if it is None:
                return
            ids = it.data(256) or []
            name = it.text().split("（")[0].strip()
            if QMessageBox.question(
                dlg,
                "删除线路",
                f"确定删除线路「{name}」及其全部 {len(ids)} 段？",
            ) != QMessageBox.Yes:
                return
            try:
                self.client.delete_track_route(list(ids))
                lst.takeItem(lst.row(it))
                left = lst.count()
                # 重读总数
                try:
                    n_seg = len(self.client.list_tracks())
                except HermesError:
                    n_seg = "?"
                lbl.setText(f"共 {left} 条线路（{n_seg} 段）")
                self._reload_tracks()
                self.status(f"已删除线路「{name}」")
            except HermesError as e:
                self.status(f"删除失败: {e}")

        btn_del.clicked.connect(do_del)
        btn_close.clicked.connect(dlg.accept)
        v = QVBoxLayout(dlg)
        v.addWidget(lbl)
        tip = QLabel("一次绘制的多段折线归为一条线路；删除将移除该线路全部线段。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#9aa3b2;font-size:11px;")
        v.addWidget(tip)
        v.addWidget(lst, 1)
        row = QHBoxLayout()
        row.addWidget(btn_del)
        row.addWidget(btn_close)
        v.addLayout(row)
        dlg.exec_()

    # ---- 遥控 / 调速 (功能表 #5 #6) ----

    def on_move_tick(self, direction: int) -> None:
        """遥控连发: 周期收到方向 -> move_by（带 UI 速度）。"""
        if not self.client:
            return
        # 急停激活时拒绝发送运动指令, 并停止连发定时器
        if self.estop_bus.latched:
            self.control._stop()
            return
        try:
            ratio = self.control.teleop_speed_ratio(direction)
            # 同时传 speed_ratio(比例) 与绝对速度, 由固件选用;
            # 实测 speed_ratio 在部分固件无效, 绝对速度字段更可靠。
            if direction in (DIR_TURN_LEFT, DIR_TURN_RIGHT):
                self.client.move_by(
                    direction,
                    speed_ratio=ratio,
                    angular_velocity=self.control.current_angular_rps(),
                )
            else:
                self.client.move_by(
                    direction,
                    speed_ratio=ratio,
                    linear_velocity=self.control.current_linear_mps(),
                )
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
            # 急停前记录臂连接态；Elite writeIdle 后仅靠 clear 无法恢复 servoj
            self._arm_was_connected_at_estop = bool(self.arm.is_connected())
            self.arm_worker.set_streaming(False)
            self.arm_panel.chk_stream.blockSignals(True)
            self.arm_panel.chk_stream.setChecked(False)
            self.arm_panel.chk_stream.blockSignals(False)
            self.estop_bus.trigger()
            dlg = getattr(self, "_arm_seq_dlg", None)
            if dlg is not None and dlg.is_runner_active():
                dlg.force_stop_runner(log_stop=True)
            self.control.set_estop_state(True)
            app_log.log_warn("estop", "系统急停已触发")
            self.status("已触发系统急停", pin_secs=8.0)
            return
        self.estop_bus.release()
        self.control.set_estop_state(False)
        app_log.log_info("estop", "软件急停已解除")
        if self._arm_was_connected_at_estop:
            # 与手动「重新连接」等价：close + EliteDriver/脚本/hold servoj
            self._recover_arm_after_estop()
        else:
            self.status("已解除软件急停", pin_secs=4.0)

    def _recover_arm_after_estop(self) -> None:
        """急停解除后重连机械臂（writeIdle 后必须完整重建外部控制会话）。"""
        self.status("急停已解除，正在重新连接机械臂…", pin_secs=8.0)
        try:
            QApplication.processEvents()
        except Exception:
            pass
        # 先停流控，避免重连窗口内 worker 继续下发
        self.arm_worker.set_streaming(False)
        try:
            self.arm.disconnect()
        except Exception:
            pass
        # on_arm_connect → ArmController.connect → elite close+reconnect+seed
        self.on_arm_connect()
        if self.arm.is_connected():
            app_log.log_info("estop", "急停解除后机械臂已重连并开流控")
            self.status("已解除急停；机械臂已重新连接", pin_secs=6.0)
        else:
            detail = self.arm.last_connect_error() or "未知错误"
            app_log.log_error("estop", f"急停解除后臂重连失败: {detail}")
            self.status(f"已解除急停，但机械臂重连失败: {detail}", pin_secs=8.0)
        self._arm_was_connected_at_estop = False

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
        self._enter_map_mode(
            lambda: self.canvas.set_wall_mode(True),
            "画虚拟墙: 按住拖拽画线，完成后退出；Esc 可中途取消",
        )

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
        self._enter_map_mode(
            lambda: self.canvas.set_reloc_mode(True),
            "重定位: 按住实际位置并拖拽朝向后松开；Esc 退出",
        )

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
        self._enter_map_mode(
            lambda: self.canvas.set_nav_mode(True),
            "导航(连续选点): 点击地图前往下一点，Esc 退出",
        )

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
        """约 2s 节流: 底盘健康角标 + 诊断面板自动刷新。"""
        now = int(time.monotonic())
        if self._last_health_check is not None and now - self._last_health_check < 2:
            return
        self._last_health_check = now
        if self.client:
            try:
                h = self.client.get_health_items()
            except HermesError:
                h = None
            if h is not None:
                errs = h.get("errors", [])
                n_err = sum(1 for e in errs if e.get("level", 0) >= 2)
                n_warn = sum(1 for e in errs if e.get("level", 0) == 1)
                self._set_health_button(n_err, n_warn)
                # 同步急停按钮(物理急停或他处触发时, 界面也反映)
                # 软件急停已 latch 时不覆盖按钮状态, 避免物理急停解除后错误清除 UI
                hw_estop = h.get("emergency_stop", False)
                if not self.estop_bus.latched:
                    self.control.set_estop_state(hw_estop)
                try:
                    q = self.client.get_localization_quality()
                    if isinstance(q, int) and q < self.LOC_QUALITY_WARN:
                        self._loc_quality_hint = f"  ⚠定位质量低({q})"
                    else:
                        self._loc_quality_hint = ""
                except HermesError:
                    self._loc_quality_hint = ""
        # 诊断 Tab 与健康轮询同步（无底盘时也刷新臂/相机）
        self.refresh_diagnosis()

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

    # ---- 定时轮询 (功能表 #2 #15) ----

    def poll(self) -> None:
        if getattr(self, "_boot_busy", False) or getattr(self, "_map_loading", False):
            return
        if not self.client:
            # 无底盘：仍节流刷新诊断（臂/相机断流等）
            self._poll_health()
            self._check_mission_schedule()
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
        self._check_mission_schedule()

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
    app_log.setup_logging()
    app = QApplication(sys.argv)
    apply_theme(app)

    # 先保证 exe 旁 config/ 骨架，再加载（frozen 的 data_dir 指向 config/data）
    try:
        PackManager().ensure_layout(seed_from_home=True)
    except Exception as e:
        app_log.log_warn("pack", f"ensure_layout at boot: {e}")

    cfg = load_devices_config()
    cfg.ensure_data_dir()
    try:
        from pathlib import Path as _Path
        from devices.config_loader import default_config_path, _find_local_overlay
        primary = default_config_path()
        local = _find_local_overlay(primary)
        rtsi_ok = all(
            _Path(p).is_file()
            for p in (cfg.arm.rtsi_output_recipe, cfg.arm.rtsi_input_recipe)
        )
        app_log.log_info(
            "config",
            f"primary={primary} local={local or '-'} "
            f"data_dir={cfg.data_dir} pack={default_pack_dir()} "
            f"arm.kind={cfg.arm.kind} arm.host={cfg.arm.host} "
            f"pc.local_ip={cfg.pc_local_ip} rtsi_files_ok={rtsi_ok}",
        )
    except Exception as e:
        app_log.log_warn("config", f"config summary failed: {e}")

    host, port = cfg.chassis.host, cfg.chassis.port
    offline = False
    ask = False
    args = [a for a in sys.argv[1:] if a]
    for arg in args:
        if arg in ("--offline", "offline"):
            offline = True
            host = ""
        elif arg in ("--ask", "ask"):
            ask = True
        elif ":" in arg and not arg.startswith("-"):
            host, p = arg.rsplit(":", 1)
            port = int(p) if p.isdigit() else cfg.chassis.port
        elif not arg.startswith("-"):
            host = arg

    # 默认静默连接；仅 --ask 时弹出原连接对话框
    if ask and host and not offline:
        dlg = ConnectDialog(host=host, port=port)
        if dlg.exec_() == ConnectDialog.Accepted:
            host, port = dlg.host(), dlg.port()
        else:
            offline = True
            host = ""

    win = MainWindow(
        host=host,
        port=port,
        map_file=_default_map_file(),
        devices_cfg=cfg,
        auto_connect=not offline,
    )
    win.show()
    win.raise_()
    win.activateWindow()
    # 先完成首绘，再启动静默连接（避免构造/show 阶段同步连设备导致白屏）
    app.processEvents()
    win.mark_ui_ready()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
