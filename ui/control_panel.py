"""遥控 + 调速面板 (功能表 #5 #6)。

遥控 (MoveByAction):
    方向键(前/后/左转/右转)按住时, 用 QTimer 周期(~150ms)重复发 move_by;
    松开即停发并 cancel 当前 action, 底盘停下。这符合 MoveByAction
    "需周期调用维持运动"的语义。

调速:
    - 运动策略下拉 (default/depot/agile/...)
    - 最大线速度滑块 (base.max_moving_speed, m/s)
    - 最大角速度滑块 (base.max_angular_speed, rad/s)

本面板只发信号, 不直接碰网络 —— 由 MainWindow 经 HermesClient 执行。
"""

from __future__ import annotations

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from devices.chassis import DIR_BACKWARD, DIR_FORWARD, DIR_TURN_LEFT, DIR_TURN_RIGHT

TICK_MS = 150  # 遥控连发周期


class ControlPanel(QWidget):

    moveTick = pyqtSignal(int)          # 周期发某方向 (DIR_*)
    stopRequested = pyqtSignal()        # 松开 -> 停止
    strategyChanged = pyqtSignal(str)   # 切换运动策略
    maxSpeedChanged = pyqtSignal(float) # 设最大线速度 m/s
    maxAngularChanged = pyqtSignal(float)  # 设最大角速度 rad/s
    estopRequested = pyqtSignal(bool)   # 急停 True=触发 False=解除
    brakeReleaseRequested = pyqtSignal(bool)  # 刹车 True=释放 False=恢复

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(180)
        self._cur_dir = None

        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        layout.addWidget(self._build_teleop())
        layout.addWidget(self._build_speed())
        layout.addWidget(self._build_safety())
        layout.addStretch(1)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(inner)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        # 连发定时器
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._on_tick)

        self.set_online(False)

    # ---- 遥控区 ----

    def _build_teleop(self) -> QGroupBox:
        box = QGroupBox("遥控 (按住移动)")
        grid = QGridLayout(box)
        grid.setSpacing(5)
        self.btn_fwd = QPushButton("▲ 前")
        self.btn_back = QPushButton("▼ 后")
        self.btn_left = QPushButton("◀ 左")
        self.btn_right = QPushButton("▶ 右")
        self.btn_stop = QPushButton("■ 停")
        # 方向键统一最小尺寸, 网格三列等宽, 十字布局更规整不挤
        for b in (self.btn_fwd, self.btn_back, self.btn_left,
                  self.btn_right, self.btn_stop):
            b.setMinimumHeight(34)
        for c in range(3):
            grid.setColumnStretch(c, 1)
        grid.addWidget(self.btn_fwd, 0, 1)
        grid.addWidget(self.btn_left, 1, 0)
        grid.addWidget(self.btn_stop, 1, 1)
        grid.addWidget(self.btn_right, 1, 2)
        grid.addWidget(self.btn_back, 2, 1)

        self._dir_btns = {
            self.btn_fwd: DIR_FORWARD,
            self.btn_back: DIR_BACKWARD,
            # 左/右键的发送值对调(实机方向与按钮相反, 故交换)
            self.btn_left: DIR_TURN_RIGHT,
            self.btn_right: DIR_TURN_LEFT,
        }
        # 按下开始连发, 松开停止
        for btn, d in self._dir_btns.items():
            btn.pressed.connect(lambda dd=d: self._start(dd))
            btn.released.connect(self._stop)
        self.btn_stop.clicked.connect(self._stop)
        return box

    def _start(self, direction: int) -> None:
        self._cur_dir = direction
        self.moveTick.emit(direction)   # 立即发一次, 不等首个 tick
        self._timer.start()

    def _stop(self) -> None:
        if self._timer.isActive():
            self._timer.stop()
        self._cur_dir = None
        self.stopRequested.emit()

    def _on_tick(self) -> None:
        if self._cur_dir is not None:
            self.moveTick.emit(self._cur_dir)

    # ---- 调速区 ----

    def _build_speed(self) -> QGroupBox:
        box = QGroupBox("速度")
        v = QVBoxLayout(box)

        v.addWidget(QLabel("运动策略"))
        self.cmb_strategy = QComboBox()
        self.cmb_strategy.currentTextChanged.connect(self._on_strategy)
        v.addWidget(self.cmb_strategy)

        # 线速度滑块 0.1~1.2 m/s (×100 取整)
        # 同时驱动：1) 底盘参数 base.max_moving_speed  2) 遥控 MoveBy speed_ratio
        self.lbl_lin = QLabel("最大线速度: -- m/s")
        v.addWidget(self.lbl_lin)
        self.sld_lin = QSlider(Qt.Horizontal)
        self.sld_lin.setRange(10, 120)
        self.sld_lin.valueChanged.connect(self._on_lin_changed)
        self.sld_lin.sliderReleased.connect(self._emit_lin)
        v.addWidget(self.sld_lin)

        # 角速度滑块 0.2~0.8 rad/s（本机 Hermes 实测写入上限约 0.8）
        self.lbl_ang = QLabel("最大角速度: -- rad/s")
        v.addWidget(self.lbl_ang)
        self.sld_ang = QSlider(Qt.Horizontal)
        self.sld_ang.setRange(20, 80)
        self.sld_ang.valueChanged.connect(self._on_ang_changed)
        self.sld_ang.sliderReleased.connect(self._emit_ang)
        v.addWidget(self.sld_ang)
        hint = QLabel("提示: 松开滑块写入底盘参数；遥控时立即按比例生效")
        hint.setStyleSheet("color: #9aa3b2; font-size: 11px;")
        hint.setWordWrap(True)
        v.addWidget(hint)
        return box

    def _on_lin_changed(self, val: int) -> None:
        self.lbl_lin.setText(f"最大线速度: {val / 100:.2f} m/s")
        if self._online and not self.sld_lin.isSliderDown():
            self._emit_lin()

    def _on_ang_changed(self, val: int) -> None:
        self.lbl_ang.setText(f"最大角速度: {val / 100:.2f} rad/s")
        if self._online and not self.sld_ang.isSliderDown():
            self._emit_ang()

    def _emit_lin(self) -> None:
        self.maxSpeedChanged.emit(self.sld_lin.value() / 100.0)

    def _emit_ang(self) -> None:
        self.maxAngularChanged.emit(self.sld_ang.value() / 100.0)

    def current_linear_mps(self) -> float:
        return self.sld_lin.value() / 100.0

    def current_angular_rps(self) -> float:
        return self.sld_ang.value() / 100.0

    def teleop_speed_ratio(self, direction: int) -> float:
        """将 UI 速度映射为 MoveByAction 的 speed_ratio (0~1)。"""
        # 转向用角速度滑块，前后用线速度滑块
        if direction in (DIR_TURN_LEFT, DIR_TURN_RIGHT):
            return max(0.05, min(1.0, self.current_angular_rps() / 0.8))
        return max(0.05, min(1.0, self.current_linear_mps() / 1.2))

    def _on_strategy(self, text: str) -> None:
        if text and self._online:
            self.strategyChanged.emit(text)

    # ---- 安全区 (急停 / 刹车释放) ----

    def _build_safety(self) -> QGroupBox:
        box = QGroupBox("安全")
        v = QVBoxLayout(box)
        # 急停: 醒目红按钮, 可切换触发/解除
        self.btn_estop = QPushButton("⏻ 急停")
        self.btn_estop.setObjectName("danger")
        self.btn_estop.setCheckable(True)
        self.btn_estop.setMinimumHeight(38)
        self.btn_estop.clicked.connect(self._on_estop_clicked)
        v.addWidget(self.btn_estop)
        # 刹车释放: 勾选=可手推
        self.chk_brake = QCheckBox("释放刹车(可手推底盘)")
        self.chk_brake.toggled.connect(self._on_brake_toggled)
        v.addWidget(self.chk_brake)
        return box

    def _on_estop_clicked(self) -> None:
        # checked 态 = 已触发急停; 发对应信号(MainWindow 做确认)
        self.estopRequested.emit(self.btn_estop.isChecked())

    def _on_brake_toggled(self, on: bool) -> None:
        self.brakeReleaseRequested.emit(on)

    def set_estop_state(self, triggered: bool) -> None:
        """同步急停按钮显示(由 MainWindow 据底盘状态或操作结果调用)。"""
        self.btn_estop.blockSignals(True)
        self.btn_estop.setChecked(triggered)
        self.btn_estop.setText("⏻ 急停中(点击解除)" if triggered else "⏻ 急停")
        self.btn_estop.blockSignals(False)

    # ---- 状态填充 (由 MainWindow 连接后调用) ----

    def set_strategies(self, strategies: list, current: str = "") -> None:
        self.cmb_strategy.blockSignals(True)
        self.cmb_strategy.clear()
        self.cmb_strategy.addItems([str(s) for s in strategies])
        if current:
            self.cmb_strategy.setCurrentText(current)
        self.cmb_strategy.blockSignals(False)

    def set_speeds(self, linear: float, angular: float) -> None:
        self.sld_lin.blockSignals(True)
        self.sld_lin.setValue(int(round(max(0.1, min(1.2, linear)) * 100)))
        self.lbl_lin.setText(f"最大线速度: {linear:.2f} m/s")
        self.sld_lin.blockSignals(False)
        self.sld_ang.blockSignals(True)
        self.sld_ang.setValue(int(round(max(0.2, min(0.8, angular)) * 100)))
        self.lbl_ang.setText(f"最大角速度: {angular:.2f} rad/s")
        self.sld_ang.blockSignals(False)

    def set_online(self, online: bool) -> None:
        self._online = online
        for w in (self.btn_fwd, self.btn_back, self.btn_left, self.btn_right,
                  self.btn_stop, self.cmb_strategy, self.sld_lin, self.sld_ang,
                  self.btn_estop, self.chk_brake):
            w.setEnabled(online)
        if not online:
            self._stop()
