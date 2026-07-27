"""诊断面板。"""

from __future__ import annotations

from typing import List

from PyQt5.QtWidgets import QLabel, QListWidget, QPushButton, QVBoxLayout, QWidget

from devices.common.status import DeviceStatus


class DiagnosisPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.addWidget(QLabel("设备诊断"))
        self.list = QListWidget()
        root.addWidget(self.list, 1)
        self.btn_refresh = QPushButton("刷新")
        root.addWidget(self.btn_refresh)

    def set_statuses(self, statuses: List[DeviceStatus]) -> None:
        self.list.clear()
        for s in statuses:
            alarms = "; ".join(a.message for a in s.alarms) if s.alarms else "-"
            self.list.addItem(
                f"[{s.device.value}] {s.state.value} | {s.detail} | 告警: {alarms}"
            )
