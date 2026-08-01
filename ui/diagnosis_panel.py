"""诊断面板。"""

from __future__ import annotations

from typing import List, Sequence

from PyQt5.QtWidgets import QLabel, QListWidget, QPushButton, QVBoxLayout, QWidget

from devices.common.status import ConnectionState, DeviceId, DeviceStatus

_DEVICE_CN = {
    DeviceId.CHASSIS: "底盘",
    DeviceId.ARM: "机械臂",
    DeviceId.CAMERA: "摄像头",
    DeviceId.RANGING: "测距",
}
_STATE_CN = {
    ConnectionState.CONNECTED: "已连接",
    ConnectionState.DISCONNECTED: "未连接",
    ConnectionState.CONNECTING: "连接中",
    ConnectionState.ERROR: "异常",
}


class DiagnosisPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.addWidget(QLabel("设备诊断"))
        self.lbl_boot = QLabel("")
        self.lbl_boot.setWordWrap(True)
        self.lbl_boot.setStyleSheet("color:#9aa3b2;font-size:11px;")
        root.addWidget(self.lbl_boot)
        self.list = QListWidget()
        root.addWidget(self.list, 1)
        self.btn_refresh = QPushButton("刷新")
        root.addWidget(self.btn_refresh)

    def set_boot_notes(self, notes: Sequence[str]) -> None:
        if not notes:
            self.lbl_boot.setText("")
            return
        self.lbl_boot.setText("启动连接：" + "；".join(notes))

    def set_statuses(self, statuses: List[DeviceStatus]) -> None:
        self.list.clear()
        for s in statuses:
            name = _DEVICE_CN.get(s.device, s.device.value)
            state = _STATE_CN.get(s.state, s.state.value)
            alarms = "; ".join(a.message for a in s.alarms) if s.alarms else "-"
            self.list.addItem(f"[{name}] {state} | {s.detail} | 告警: {alarms}")
