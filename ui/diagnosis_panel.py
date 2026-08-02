"""诊断面板：综合健康、设备运行态、日志导出。"""

from __future__ import annotations

from typing import List, Optional, Sequence

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from devices.common.status import (
    AlarmLevel,
    ConnectionState,
    DeviceId,
    DeviceStatus,
    HealthSummary,
    OverallHealth,
)

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
_OVERALL_CN = {
    OverallHealth.OK: "正常",
    OverallHealth.DEGRADED: "降级",
    OverallHealth.FAULT: "故障",
}
_LEVEL_COLOR = {
    AlarmLevel.CRITICAL: "#f85149",
    AlarmLevel.ERROR: "#f85149",
    AlarmLevel.WARN: "#e0c055",
    AlarmLevel.INFO: "#9aa3b2",
}


class DiagnosisPanel(QWidget):
    exportRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.addWidget(QLabel("设备诊断"))

        self.lbl_summary = QLabel("综合健康：—")
        self.lbl_summary.setWordWrap(True)
        self.lbl_summary.setStyleSheet("font-size:13px; font-weight:600;")
        root.addWidget(self.lbl_summary)

        self.lbl_boot = QLabel("")
        self.lbl_boot.setWordWrap(True)
        self.lbl_boot.setStyleSheet("color:#9aa3b2;font-size:11px;")
        root.addWidget(self.lbl_boot)

        self.list = QListWidget()
        root.addWidget(self.list, 1)

        row = QHBoxLayout()
        self.btn_refresh = QPushButton("刷新")
        self.btn_export = QPushButton("导出日志")
        row.addWidget(self.btn_refresh)
        row.addWidget(self.btn_export)
        root.addLayout(row)

        self.btn_export.clicked.connect(self.exportRequested.emit)

    def set_boot_notes(self, notes: Sequence[str]) -> None:
        if not notes:
            self.lbl_boot.setText("")
            return
        self.lbl_boot.setText("启动连接：" + "；".join(notes))

    def set_summary(self, summary: Optional[HealthSummary]) -> None:
        if summary is None:
            self.lbl_summary.setText("综合健康：—")
            self.lbl_summary.setStyleSheet("font-size:13px; font-weight:600;")
            return
        label = _OVERALL_CN.get(summary.overall, summary.overall.value)
        self.lbl_summary.setText(
            f"综合健康：{label}  |  故障 {summary.fault_count}  警告 {summary.warn_count}"
        )
        if summary.overall == OverallHealth.FAULT:
            color = "#f85149"
        elif summary.overall == OverallHealth.DEGRADED:
            color = "#e0c055"
        else:
            color = "#3fb950"
        self.lbl_summary.setStyleSheet(
            f"font-size:13px; font-weight:600; color:{color};"
        )

    def set_statuses(
        self,
        statuses: List[DeviceStatus],
        summary: Optional[HealthSummary] = None,
    ) -> None:
        if summary is not None:
            self.set_summary(summary)
        self.list.clear()
        for s in statuses:
            name = _DEVICE_CN.get(s.device, s.device.value)
            state = _STATE_CN.get(s.state, s.state.value)
            ok_tag = "正常" if s.ok else "异常"
            line = f"[{name}] {state} · {ok_tag} | {s.detail}"
            item = QListWidgetItem(line)
            if s.state == ConnectionState.ERROR or not s.ok:
                item.setForeground(QColor("#f85149"))
            elif s.state == ConnectionState.CONNECTING:
                item.setForeground(QColor("#e0c055"))
            elif s.state == ConnectionState.CONNECTED:
                item.setForeground(QColor("#3fb950"))
            else:
                item.setForeground(QColor("#9aa3b2"))
            self.list.addItem(item)

            if s.metrics:
                parts = []
                for k, v in s.metrics.items():
                    parts.append(f"{k}={v}")
                m_item = QListWidgetItem("    指标: " + ", ".join(parts))
                m_item.setForeground(QColor("#9aa3b2"))
                self.list.addItem(m_item)

            if s.alarms:
                for a in s.alarms:
                    color = _LEVEL_COLOR.get(a.level, "#9aa3b2")
                    a_item = QListWidgetItem(
                        f"    ⚠ [{a.level.value}] {a.code}: {a.message}"
                    )
                    a_item.setForeground(QColor(color))
                    self.list.addItem(a_item)
            else:
                none_item = QListWidgetItem("    告警: -")
                none_item.setForeground(QColor("#5b6370"))
                self.list.addItem(none_item)
