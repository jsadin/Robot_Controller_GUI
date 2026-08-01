"""Mission（任务组）面板 —— 承接原「任务」栏巡检 + 跨设备步骤。"""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.mission import Mission, MissionStatus, ScheduleKind, step_summary
from ui.mission_edit_dialog import MissionEditDialog, PoiChoice


def _sched_text(m: Mission) -> str:
    if m.schedule_kind == ScheduleKind.DAILY:
        return f"每日 {m.schedule_time}"
    if m.schedule_kind == ScheduleKind.ONCE:
        return f"单次 {m.schedule_time}"
    return "手动"


class MissionPanel(QWidget):
    runRequested = pyqtSignal(object)  # Mission
    pauseRequested = pyqtSignal()
    resumeRequested = pyqtSignal(object)  # Mission
    abortRequested = pyqtSignal()
    refreshRequested = pyqtSignal()
    saveRequested = pyqtSignal(object)  # Mission
    deleteRequested = pyqtSignal(int)

    def __init__(
        self,
        get_pois: Optional[Callable[[], Sequence[PoiChoice]]] = None,
        get_sequences: Optional[Callable[[], Sequence[str]]] = None,
        get_sequence_poses: Optional[Callable[[str], Sequence[str]]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.get_pois = get_pois or (lambda: [])
        self.get_sequences = get_sequences or (lambda: [])
        self.get_sequence_poses = get_sequence_poses or (lambda _n: [])

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.addWidget(QLabel("任务组（巡检 + 臂 + 相机）"))
        self.list = QListWidget()
        root.addWidget(self.list, 1)

        self.lbl_detail = QLabel("选中任务组可查看步骤")
        self.lbl_detail.setWordWrap(True)
        self.lbl_detail.setStyleSheet("color:#aaa;font-size:11px;")
        root.addWidget(self.lbl_detail)

        self.lbl_progress = QLabel("")
        self.lbl_progress.setWordWrap(True)
        self.lbl_progress.setStyleSheet("color:#8cf;font-size:11px;")
        root.addWidget(self.lbl_progress)

        row1 = QHBoxLayout()
        self.btn_new = QPushButton("新建")
        self.btn_edit = QPushButton("编辑")
        self.btn_del = QPushButton("删除")
        self.btn_refresh = QPushButton("刷新")
        row1.addWidget(self.btn_new)
        row1.addWidget(self.btn_edit)
        row1.addWidget(self.btn_del)
        row1.addWidget(self.btn_refresh)
        root.addLayout(row1)

        row2 = QHBoxLayout()
        self.btn_run = QPushButton("执行")
        self.btn_run.setObjectName("primary")
        self.btn_pause = QPushButton("暂停")
        self.btn_resume = QPushButton("恢复")
        self.btn_abort = QPushButton("中断")
        self.btn_abort.setObjectName("danger")
        row2.addWidget(self.btn_run)
        row2.addWidget(self.btn_pause)
        row2.addWidget(self.btn_resume)
        row2.addWidget(self.btn_abort)
        root.addLayout(row2)

        self._missions: list = []
        self.btn_new.clicked.connect(self._on_new)
        self.btn_edit.clicked.connect(self._on_edit)
        self.btn_del.clicked.connect(self._on_delete)
        self.btn_run.clicked.connect(self._emit_run)
        self.btn_pause.clicked.connect(self.pauseRequested.emit)
        self.btn_resume.clicked.connect(self._emit_resume)
        self.btn_abort.clicked.connect(self.abortRequested.emit)
        self.btn_refresh.clicked.connect(self.refreshRequested.emit)
        self.list.currentRowChanged.connect(self._on_sel)

    @staticmethod
    def display_step_fraction(cur_idx: int, total: int, status: str) -> str:
        """列表/进度用的步数文案：执行中为 1-based 当前步；完成后 n/n。"""
        n = max(0, int(total))
        if n <= 0:
            return "0/0"
        if status == MissionStatus.DONE:
            return f"{n}/{n}"
        # 执行中/暂停/中断：cur_idx 为当前步 0-based；完成后 executor 会把 cur_idx 设为 n
        if cur_idx >= n:
            return f"{n}/{n}"
        return f"{cur_idx + 1}/{n}"

    def set_missions(self, missions) -> None:
        sel_id = None
        row = self.list.currentRow()
        if 0 <= row < len(self._missions):
            sel_id = self._missions[row].id
        self._missions = list(missions)
        self.list.clear()
        restore = 0
        for i, m in enumerate(self._missions):
            n = len(m.steps)
            frac = self.display_step_fraction(m.cur_idx, n, m.status)
            self.list.addItem(
                f"#{m.id} {m.name}\n[{m.status}] {frac} · {_sched_text(m)}"
            )
            if sel_id is not None and m.id == sel_id:
                restore = i
        if self._missions:
            self.list.setCurrentRow(restore)
        else:
            self.lbl_detail.setText("暂无任务组，点「新建」编排巡检/跨设备步骤")
        self._on_sel(self.list.currentRow())

    def set_progress_text(self, text: str) -> None:
        self.lbl_progress.setText(text or "")

    def current_mission(self) -> Optional[Mission]:
        row = self.list.currentRow()
        if 0 <= row < len(self._missions):
            return self._missions[row]
        return None

    def _step_mark(self, i: int, m: Mission) -> str:
        n = len(m.steps)
        if m.status == MissionStatus.DONE:
            return "✓"
        if m.status in (MissionStatus.RUNNING, MissionStatus.PAUSED):
            if i == m.cur_idx and 0 <= i < n:
                return "▶"
            if i < m.cur_idx:
                return "✓"
            return "·"
        if m.status == MissionStatus.ABORTED:
            if i == m.cur_idx and 0 <= i < n:
                return "■"
            if i < m.cur_idx:
                return "✓"
            return "·"
        return "·"

    def _on_sel(self, row: int) -> None:
        if row < 0 or row >= len(self._missions):
            self.lbl_detail.setText("选中任务组可查看步骤")
            return
        m = self._missions[row]
        lines = [f"「{m.name}」· {_sched_text(m)}"]
        if m.reason:
            lines.append(f"备注: {m.reason}")
        if not m.steps:
            lines.append("（无步骤）")
        else:
            for i, s in enumerate(m.steps):
                mark = self._step_mark(i, m)
                lines.append(f"  {mark} {i + 1}. {step_summary(s)}")
        self.lbl_detail.setText("\n".join(lines))

    def _open_editor(self, mission: Optional[Mission]) -> None:
        dlg = MissionEditDialog(
            mission=mission,
            get_pois=self.get_pois,
            get_sequences=self.get_sequences,
            get_sequence_poses=self.get_sequence_poses,
            parent=self,
        )
        if dlg.exec_() == MissionEditDialog.Accepted:
            self.saveRequested.emit(dlg.result_mission())

    def _on_new(self) -> None:
        self._open_editor(None)

    def _on_edit(self) -> None:
        m = self.current_mission()
        if m is None:
            QMessageBox.information(self, "任务组", "请先选中一个任务组。")
            return
        if m.status == MissionStatus.RUNNING:
            QMessageBox.information(self, "任务组", "执行中不可编辑，请先暂停或中断。")
            return
        self._open_editor(m)

    def _on_delete(self) -> None:
        m = self.current_mission()
        if m is None or m.id is None:
            QMessageBox.information(self, "任务组", "请先选中一个任务组。")
            return
        if m.status == MissionStatus.RUNNING:
            QMessageBox.information(self, "任务组", "执行中不可删除，请先中断。")
            return
        if (
            QMessageBox.question(
                self, "任务组", f"删除任务组「{m.name}」？", QMessageBox.Yes | QMessageBox.No
            )
            != QMessageBox.Yes
        ):
            return
        self.deleteRequested.emit(int(m.id))

    def _emit_run(self) -> None:
        m = self.current_mission()
        if m is None:
            QMessageBox.information(self, "任务组", "请先选中一个任务组。")
            return
        self.runRequested.emit(m)

    def _emit_resume(self) -> None:
        m = self.current_mission()
        if m is None:
            QMessageBox.information(self, "任务组", "请先选中一个任务组。")
            return
        self.resumeRequested.emit(m)
