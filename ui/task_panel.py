"""任务面板 (功能表 #12/#13/#14) —— 第三个标签页。

任务列表 + 控制按钮。面板只发信号, 不碰网络/不碰底盘/不碰 store ——
由 MainWindow 统一驱动 executor 与 store。
"""

from __future__ import annotations

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class TaskPanel(QWidget):

    newTask = pyqtSignal()             # 新建
    editTask = pyqtSignal(int)         # 编辑 task_id
    deleteTask = pyqtSignal(int)       # 删除 task_id
    startTask = pyqtSignal(int)        # 开始 task_id
    pauseTask = pyqtSignal()           # 暂停当前
    resumeTask = pyqtSignal(int)       # 恢复 task_id
    abortTask = pyqtSignal()           # 中断当前

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(236)

        self.list = QListWidget()
        self.btn_new = QPushButton("✛ 新建")
        self.btn_new.setObjectName("primary")
        self.btn_edit = QPushButton("✎ 编辑")
        self.btn_del = QPushButton("🗑 删除")
        self.btn_del.setObjectName("danger")
        self.btn_start = QPushButton("▶ 开始")
        self.btn_start.setObjectName("primary")
        self.btn_pause = QPushButton("⏸ 暂停")
        self.btn_resume = QPushButton("⏵ 恢复")
        self.btn_abort = QPushButton("⏹ 中断")
        self.btn_abort.setObjectName("danger")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        title = QLabel("任务列表")
        title.setStyleSheet("font-weight: bold; color: #9aa3b2;")
        layout.addWidget(title)
        layout.addWidget(self.list, 1)
        row1 = QHBoxLayout()
        row1.addWidget(self.btn_new)
        row1.addWidget(self.btn_edit)
        row1.addWidget(self.btn_del)
        layout.addLayout(row1)
        layout.addWidget(self.btn_start)
        row2 = QHBoxLayout()
        row2.addWidget(self.btn_pause)
        row2.addWidget(self.btn_resume)
        layout.addLayout(row2)
        layout.addWidget(self.btn_abort)

        self.btn_new.clicked.connect(self.newTask)
        self.btn_edit.clicked.connect(self._emit_edit)
        self.btn_del.clicked.connect(self._emit_del)
        self.btn_start.clicked.connect(self._emit_start)
        self.btn_pause.clicked.connect(self.pauseTask)
        self.btn_resume.clicked.connect(self._emit_resume)
        self.btn_abort.clicked.connect(self.abortTask)
        self.list.itemSelectionChanged.connect(self._update_buttons)

        self._online = False
        self.set_online(False)

    # ---- 数据 ----

    def set_tasks(self, tasks) -> None:
        prev = self.current_task_id()
        self.list.blockSignals(True)
        self.list.clear()
        for t in tasks:
            label = f"{t.name}  [{t.status} {t.progress()}]"
            if t.schedule_kind != "none":
                label += f"  ⏰{t.schedule_time}"
            it = QListWidgetItem(label)
            it.setData(256, t.id)
            self.list.addItem(it)
        self.list.blockSignals(False)
        if prev is not None:
            self.select_task(prev)
        self._update_buttons()

    def current_task_id(self):
        it = self.list.currentItem()
        return it.data(256) if it is not None else None

    def select_task(self, task_id) -> None:
        for i in range(self.list.count()):
            if self.list.item(i).data(256) == task_id:
                self.list.setCurrentRow(i)
                return

    # ---- 状态 ----

    def set_online(self, online: bool) -> None:
        self._online = online
        self._update_buttons()

    def _update_buttons(self) -> None:
        has = self.current_task_id() is not None
        on = self._online
        self.btn_new.setEnabled(on)
        for b in (self.btn_edit, self.btn_del, self.btn_start,
                  self.btn_pause, self.btn_resume, self.btn_abort):
            b.setEnabled(on and has)

    # ---- 内部 ----

    def _emit_edit(self):
        tid = self.current_task_id()
        if tid is not None:
            self.editTask.emit(tid)

    def _emit_del(self):
        tid = self.current_task_id()
        if tid is not None:
            self.deleteTask.emit(tid)

    def _emit_start(self):
        tid = self.current_task_id()
        if tid is not None:
            self.startTask.emit(tid)

    def _emit_resume(self):
        tid = self.current_task_id()
        if tid is not None:
            self.resumeTask.emit(tid)
