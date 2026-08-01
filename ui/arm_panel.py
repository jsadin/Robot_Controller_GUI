"""机械臂面板（PyQt5，风格对齐底盘主题）。"""

from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)


class ArmPanel(QWidget):
    connectRequested = pyqtSignal()
    disconnectRequested = pyqtSignal()
    jointsChanged = pyqtSignal(list)  # 6 deg
    streamToggled = pyqtSignal(bool)
    speedChanged = pyqtSignal(float)
    speedLimitToggled = pyqtSignal(bool)
    sequencesRequested = pyqtSignal()
    brakeReleaseRequested = pyqtSignal()  # stub

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(180)
        self._sliders = []
        self._updating = False

        inner = QWidget()
        root = QVBoxLayout(inner)
        root.setContentsMargins(8, 8, 8, 8)

        conn = QHBoxLayout()
        self.btn_connect = QPushButton("连接机械臂")
        self.btn_connect.setObjectName("primary")
        self.btn_disconnect = QPushButton("断开")
        self.lbl_state = QLabel("未连接")
        self.lbl_state.setStyleSheet("color: #9aa3b2;")
        conn.addWidget(self.btn_connect)
        conn.addWidget(self.btn_disconnect)
        conn.addWidget(self.lbl_state, 1)
        root.addLayout(conn)

        box = QGroupBox("关节调节 (°)")
        form = QFormLayout(box)
        for i in range(6):
            row = QHBoxLayout()
            sl = QSlider(Qt.Horizontal)
            sl.setRange(-3600, 3600)  # 0.1 deg
            sl.setValue(0)
            lab = QLabel("0.0")
            lab.setMinimumWidth(48)
            sl.valueChanged.connect(self._on_slider)
            row.addWidget(sl, 1)
            row.addWidget(lab)
            form.addRow(f"J{i + 1}", row)
            self._sliders.append((sl, lab))
        root.addWidget(box)

        self.chk_stream = QCheckBox("启用关节流控 (~50Hz)")
        root.addWidget(self.chk_stream)

        spd = QGroupBox("关节调速")
        sf = QFormLayout(spd)
        self.spin_speed = QDoubleSpinBox()
        self.spin_speed.setRange(1.0, 180.0)
        self.spin_speed.setValue(45.0)
        self.spin_speed.setSuffix(" °/s")
        self.chk_limit = QCheckBox("启用速度限制")
        self.chk_limit.setChecked(True)
        sf.addRow("最大角速度", self.spin_speed)
        sf.addRow(self.chk_limit)
        root.addWidget(spd)

        acts = QHBoxLayout()
        self.btn_seq = QPushButton("动作组…")
        self.btn_brake = QPushButton("手动释放(预留)")
        self.btn_brake.setEnabled(False)
        self.btn_brake.setToolTip("预留抱闸解锁接口")
        acts.addWidget(self.btn_seq)
        acts.addWidget(self.btn_brake)
        root.addLayout(acts)
        root.addStretch(1)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(inner)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        self.btn_connect.clicked.connect(self.connectRequested.emit)
        self.btn_disconnect.clicked.connect(self.disconnectRequested.emit)
        self.chk_stream.toggled.connect(self.streamToggled.emit)
        self.spin_speed.valueChanged.connect(self.speedChanged.emit)
        self.chk_limit.toggled.connect(self.speedLimitToggled.emit)
        self.btn_seq.clicked.connect(self.sequencesRequested.emit)
        self.btn_brake.clicked.connect(self.brakeReleaseRequested.emit)

    def _on_slider(self, _v=None) -> None:
        if self._updating:
            return
        degs = [sl.value() / 10.0 for sl, _ in self._sliders]
        for (_, lab), d in zip(self._sliders, degs):
            lab.setText(f"{d:.1f}")
        self.jointsChanged.emit(degs)

    def set_joints_deg(self, degs) -> None:
        if not degs or len(degs) != 6:
            return
        self._updating = True
        try:
            for (sl, lab), d in zip(self._sliders, degs):
                sl.setValue(int(round(float(d) * 10)))
                lab.setText(f"{float(d):.1f}")
        finally:
            self._updating = False

    def joint_values_deg(self) -> list:
        return [sl.value() / 10.0 for sl, _ in self._sliders]

    def set_connected(self, ok: bool, detail: str = "") -> None:
        self.lbl_state.setText(("已连接 " + detail).strip() if ok else (detail or "未连接"))
        self.lbl_state.setStyleSheet("color: #3fb950;" if ok else "color: #9aa3b2;")
