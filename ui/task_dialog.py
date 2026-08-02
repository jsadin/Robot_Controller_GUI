"""任务编辑对话框 —— 从星标组成有序航点序列, 并设定时。

不碰网络: 输入是当前 POI 列表, 输出是 Task 的字段(由调用方落库)。
"""

from __future__ import annotations

from PyQt5.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
)
from PyQt5.QtCore import QDate, QTime, Qt

from tasks.model import HOME_POI, ScheduleKind, Task

DWELL_ROLE = 257   # 列表项里存停留秒数的 data role


class TaskDialog(QDialog):
    """新建/编辑任务。

    左侧可选星标, 右侧航点序列(可上下移、可删); 下方设定时。
    """

    def __init__(self, pois, task: Task = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑任务" if task else "新建任务")
        self.resize(460, 420)
        self._pois = list(pois)
        self._poi_name = {str(p.poi_id): p.name for p in self._pois}

        self.name_edit = QLineEdit(task.name if task else "")

        # 左: 可选星标; 右: 航点序列
        self.src = QListWidget()
        self.src.setSelectionMode(QAbstractItemView.ExtendedSelection)
        for p in self._pois:
            it = QListWidgetItem(p.name or str(p.poi_id))
            it.setData(256, str(p.poi_id))
            self.src.addItem(it)

        self.seq = QListWidget()
        self.seq.setSelectionMode(QAbstractItemView.SingleSelection)

        btn_add = QPushButton("加入 →")
        btn_rm = QPushButton("← 移除")
        btn_up = QPushButton("上移")
        btn_dn = QPushButton("下移")
        btn_home = QPushButton("加入回桩")
        btn_add.clicked.connect(self._add_sel)
        btn_rm.clicked.connect(self._rm_sel)
        btn_up.clicked.connect(lambda: self._move(-1))
        btn_dn.clicked.connect(lambda: self._move(1))
        btn_home.clicked.connect(self._add_home)

        mid = QVBoxLayout()
        mid.addStretch(1)
        mid.addWidget(btn_add)
        mid.addWidget(btn_rm)
        mid.addWidget(btn_up)
        mid.addWidget(btn_dn)
        mid.addWidget(btn_home)
        mid.addStretch(1)

        lists = QHBoxLayout()
        lcol = QVBoxLayout()
        lcol.addWidget(QLabel("可选星标"))
        lcol.addWidget(self.src)
        rcol = QVBoxLayout()
        rcol.addWidget(QLabel("航点序列(按顺序)"))
        rcol.addWidget(self.seq)
        # 选中航点的停留秒数编辑
        dwell_row = QHBoxLayout()
        dwell_row.addWidget(QLabel("选中点停留:"))
        self.dwell_spin = QSpinBox()
        self.dwell_spin.setRange(0, 3600)
        self.dwell_spin.setSuffix(" 秒")
        self.dwell_spin.valueChanged.connect(self._on_dwell_changed)
        dwell_row.addWidget(self.dwell_spin)
        dwell_row.addStretch(1)
        rcol.addLayout(dwell_row)
        self.seq.currentRowChanged.connect(self._on_seq_sel)
        lists.addLayout(lcol)
        lists.addLayout(mid)
        lists.addLayout(rcol)

        # 定时设置
        self.cmb_sched = QComboBox()
        self.cmb_sched.addItem("不定时", ScheduleKind.NONE)
        self.cmb_sched.addItem("每日循环", ScheduleKind.DAILY)
        self.cmb_sched.addItem("单次(日历)", ScheduleKind.ONCE)
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setDate(QDate.currentDate())
        self.time_edit = QTimeEdit()
        self.time_edit.setDisplayFormat("HH:mm")
        self.time_edit.setTime(QTime.currentTime())
        self._orig_sched_kind = task.schedule_kind if task else ScheduleKind.NONE
        self._sync_calendar_popup()
        self.cmb_sched.currentIndexChanged.connect(self._on_sched_kind)

        sched = QHBoxLayout()
        sched.addWidget(QLabel("定时:"))
        sched.addWidget(self.cmb_sched)
        sched.addWidget(QLabel("日期:"))
        sched.addWidget(self.date_edit)
        sched.addWidget(QLabel("时间:"))
        sched.addWidget(self.time_edit)
        sched.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("任务名:"))
        name_row.addWidget(self.name_edit)
        root.addLayout(name_row)
        root.addLayout(lists, 1)
        root.addLayout(sched)
        root.addWidget(buttons)

        # 编辑模式: 回填
        if task:
            for i, pid in enumerate(task.poi_ids):
                self._append_seq(pid, task.dwell_at(i))
            idx = self.cmb_sched.findData(task.schedule_kind)
            if idx >= 0:
                self.cmb_sched.setCurrentIndex(idx)
            if task.schedule_time:
                parts = task.schedule_time.split(" ")
                if len(parts) == 2:
                    qd = QDate.fromString(parts[0], "yyyy-MM-dd")
                    if qd.isValid():
                        self.date_edit.setDate(qd)
                    hhmm = parts[1]
                else:
                    hhmm = parts[-1]
                t = QTime.fromString(hhmm, "HH:mm")
                if t.isValid():
                    self.time_edit.setTime(t)
        self._on_sched_kind()

    # ---- 航点编辑 ----

    def _seq_label(self, pid: str, dwell: int) -> str:
        name = "🏠 回充电桩" if pid == HOME_POI else \
            self._poi_name.get(str(pid), str(pid))
        return f"{name}  (停留{dwell}s)" if dwell > 0 else name

    def _append_seq(self, pid: str, dwell: int = 0) -> None:
        it = QListWidgetItem(self._seq_label(pid, dwell))
        it.setData(256, str(pid))
        it.setData(DWELL_ROLE, int(dwell))
        self.seq.addItem(it)

    def _add_sel(self) -> None:
        for it in self.src.selectedItems():
            self._append_seq(it.data(256))

    def _add_home(self) -> None:
        self._append_seq(HOME_POI)

    def _rm_sel(self) -> None:
        for it in self.seq.selectedItems():
            self.seq.takeItem(self.seq.row(it))

    def _on_seq_sel(self, row: int) -> None:
        """选中航点 -> 把其停留秒数填进 spinbox。"""
        self.dwell_spin.blockSignals(True)
        it = self.seq.item(row) if row >= 0 else None
        self.dwell_spin.setEnabled(it is not None)
        self.dwell_spin.setValue(int(it.data(DWELL_ROLE)) if it else 0)
        self.dwell_spin.blockSignals(False)

    def _on_dwell_changed(self, val: int) -> None:
        """spinbox 改值 -> 写回当前选中航点并刷新显示。"""
        it = self.seq.currentItem()
        if it is None:
            return
        it.setData(DWELL_ROLE, int(val))
        it.setText(self._seq_label(it.data(256), int(val)))

    def _move(self, delta: int) -> None:
        row = self.seq.currentRow()
        if row < 0:
            return
        new = row + delta
        if 0 <= new < self.seq.count():
            it = self.seq.takeItem(row)
            self.seq.insertItem(new, it)
            self.seq.setCurrentRow(new)

    def _sync_calendar_popup(self) -> None:
        d = self.date_edit.date()
        if not d.isValid():
            d = QDate.currentDate()
            self.date_edit.setDate(d)
        cal = self.date_edit.calendarWidget()
        if cal is not None:
            cal.setSelectedDate(d)
            cal.setCurrentPage(d.year(), d.month())

    def _on_sched_kind(self) -> None:
        kind = self.cmb_sched.currentData()
        timed = kind != ScheduleKind.NONE
        self.time_edit.setEnabled(timed)
        once = kind == ScheduleKind.ONCE
        self.date_edit.setEnabled(once)
        if once:
            if self._orig_sched_kind != ScheduleKind.ONCE or not self.date_edit.date().isValid():
                self.date_edit.setDate(QDate.currentDate())
            self._sync_calendar_popup()

    # ---- 结果 ----

    def result_fields(self) -> dict:
        """返回 {name, poi_ids, dwells, schedule_kind, schedule_time}。"""
        poi_ids = [self.seq.item(i).data(256) for i in range(self.seq.count())]
        dwells = [int(self.seq.item(i).data(DWELL_ROLE) or 0)
                  for i in range(self.seq.count())]
        kind = self.cmb_sched.currentData()
        hhmm = self.time_edit.time().toString("HH:mm")
        if kind == ScheduleKind.NONE:
            stime = ""
        elif kind == ScheduleKind.DAILY:
            stime = hhmm
        else:  # ONCE: 日历日期 + HH:mm
            stime = f"{self.date_edit.date().toString('yyyy-MM-dd')} {hhmm}"
        return {
            "name": self.name_edit.text().strip() or "未命名任务",
            "poi_ids": poi_ids,
            "dwells": dwells,
            "schedule_kind": kind,
            "schedule_time": stime,
        }
