"""Mission（跨设备任务组）面板。"""

from __future__ import annotations

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.mission import Mission, MissionStatus, MissionStep


class MissionPanel(QWidget):
    runRequested = pyqtSignal(object)  # Mission
    abortRequested = pyqtSignal()
    refreshRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.addWidget(QLabel("任务组 Mission（底盘+臂+相机串行）"))
        self.list = QListWidget()
        root.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.btn_new = QPushButton("新建示例")
        self.btn_run = QPushButton("执行")
        self.btn_run.setObjectName("primary")
        self.btn_abort = QPushButton("中断")
        self.btn_abort.setObjectName("danger")
        self.btn_refresh = QPushButton("刷新")
        row.addWidget(self.btn_new)
        row.addWidget(self.btn_run)
        row.addWidget(self.btn_abort)
        row.addWidget(self.btn_refresh)
        root.addLayout(row)
        self._missions: list = []
        self.btn_run.clicked.connect(self._emit_run)
        self.btn_abort.clicked.connect(self.abortRequested.emit)
        self.btn_refresh.clicked.connect(self.refreshRequested.emit)
        self.btn_new.clicked.connect(self._new_sample)

    def set_missions(self, missions) -> None:
        self._missions = list(missions)
        self.list.clear()
        for m in self._missions:
            self.list.addItem(
                f"#{m.id} {m.name} [{m.status}] {m.cur_idx}/{len(m.steps)}"
            )

    def _emit_run(self) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self._missions):
            self.runRequested.emit(self._missions[row])

    def _new_sample(self) -> None:
        name, ok = QInputDialog.getText(self, "Mission", "名称:")
        if not ok or not name.strip():
            return
        m = Mission(
            id=None,
            name=name.strip(),
            steps=[
                MissionStep("wait", {"seconds": 1}),
                MissionStep("snapshot", {}),
            ],
            status=MissionStatus.PENDING,
        )
        self.runRequested.emit(("__create__", m))
