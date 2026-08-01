"""Mission 步骤编辑：导航/回桩/等待/动作组/抓拍 + 日历定时。"""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence, Tuple

from PyQt5.QtCore import QDate, QTime
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
)

from core.mission import Mission, MissionStep, ScheduleKind, step_summary

PoiChoice = Tuple[str, str]  # (poi_id, display_name)


class SequenceStepConfigDialog(QDialog):
    """选择动作组 + 分步抓拍策略。"""

    def __init__(
        self,
        *,
        sequence_names: Sequence[str],
        get_sequence_poses: Callable[[str], Sequence[str]],
        initial: Optional[dict] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("动作组步骤")
        self.resize(420, 360)
        self.get_sequence_poses = get_sequence_poses
        initial = initial or {}

        root = QVBoxLayout(self)
        form = QFormLayout()
        self.cmb_seq = QComboBox()
        for n in sequence_names:
            self.cmb_seq.addItem(n)
        cur = str(initial.get("sequence") or "")
        if cur:
            ix = self.cmb_seq.findText(cur)
            if ix >= 0:
                self.cmb_seq.setCurrentIndex(ix)
        form.addRow("动作组", self.cmb_seq)

        self.cmb_mode = QComboBox()
        self.cmb_mode.addItem("不抓拍", "none")
        self.cmb_mode.addItem("每一位姿后抓拍", "each_pose")
        self.cmb_mode.addItem("勾选位姿后抓拍", "selected")
        mode = str(initial.get("snapshot_mode") or "none")
        mix = self.cmb_mode.findData(mode)
        if mix >= 0:
            self.cmb_mode.setCurrentIndex(mix)
        form.addRow("抓拍", self.cmb_mode)
        root.addLayout(form)

        root.addWidget(QLabel("勾选需抓拍的位姿（仅「勾选位姿后抓拍」时有效）"))
        self.list_poses = QListWidget()
        self.list_poses.setSelectionMode(QAbstractItemView.MultiSelection)
        root.addWidget(self.list_poses, 1)

        hint = QLabel("到位姿并等待到位后抓拍，再进入步间延时。未开摄像头时抓拍失败不中断动作组。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666;font-size:10px;")
        root.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._preselect = [str(x) for x in (initial.get("snapshot_poses") or [])]
        self.cmb_seq.currentIndexChanged.connect(self._reload_poses)
        self.cmb_mode.currentIndexChanged.connect(self._on_mode)
        self._reload_poses()
        self._on_mode()

    def _on_mode(self) -> None:
        self.list_poses.setEnabled(self.cmb_mode.currentData() == "selected")

    def _reload_poses(self) -> None:
        name = self.cmb_seq.currentText().strip()
        poses = list(self.get_sequence_poses(name) or [])
        selected = set(self._preselect)
        # 切换动作组后保留同名勾选
        for i in range(self.list_poses.count()):
            it = self.list_poses.item(i)
            if it.isSelected():
                selected.add(it.text())
        self.list_poses.clear()
        for pn in poses:
            it = QListWidgetItem(pn)
            self.list_poses.addItem(it)
            if pn in selected:
                it.setSelected(True)
        self._preselect = []

    def _accept(self) -> None:
        if not self.cmb_seq.currentText().strip():
            QMessageBox.warning(self, "动作组", "请选择动作组。")
            return
        if self.cmb_mode.currentData() == "selected":
            if not self.list_poses.selectedItems():
                QMessageBox.warning(self, "动作组", "请至少勾选一个要抓拍的位姿。")
                return
        self.accept()

    def result_params(self) -> dict:
        mode = self.cmb_mode.currentData()
        poses = [it.text() for it in self.list_poses.selectedItems()]
        return {
            "sequence": self.cmb_seq.currentText().strip(),
            "snapshot_mode": mode,
            "snapshot_poses": poses if mode == "selected" else [],
        }


class MissionEditDialog(QDialog):
    def __init__(
        self,
        *,
        mission: Optional[Mission] = None,
        get_pois: Optional[Callable[[], Sequence[PoiChoice]]] = None,
        get_sequences: Optional[Callable[[], Sequence[str]]] = None,
        get_sequence_poses: Optional[Callable[[str], Sequence[str]]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑任务组" if mission and mission.id else "新建任务组")
        self.resize(560, 520)
        self.get_pois = get_pois or (lambda: [])
        self.get_sequences = get_sequences or (lambda: [])
        self.get_sequence_poses = get_sequence_poses or (lambda _n: [])
        self._mission_id = mission.id if mission else None
        self._orig_schedule = (
            (mission.schedule_kind or ScheduleKind.NONE, mission.schedule_time or "")
            if mission
            else (ScheduleKind.NONE, "")
        )
        self._keep_last_run = (mission.last_run_date if mission else "") or ""
        self._steps: List[MissionStep] = (
            [MissionStep(s.kind, dict(s.params)) for s in mission.steps]
            if mission
            else []
        )
        self._poi_name = {pid: name for pid, name in self.get_pois()}

        root = QVBoxLayout(self)
        form = QFormLayout()
        self.ed_name = QLineEdit(mission.name if mission else "")
        self.ed_name.setPlaceholderText("任务组名称")
        form.addRow("名称", self.ed_name)
        root.addLayout(form)

        # ---- 可选星标 + 步骤列表 ----
        lists = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel("可选星标（可多选）"))
        self.src = QListWidget()
        self.src.setSelectionMode(QAbstractItemView.ExtendedSelection)
        for pid, name in self.get_pois():
            it = QListWidgetItem(name or pid)
            it.setData(256, pid)
            self.src.addItem(it)
        left.addWidget(self.src)

        mid = QVBoxLayout()
        mid.addStretch(1)
        b_add_pois = QPushButton("加入导航 →")
        b_home = QPushButton("加入回桩")
        b_wait = QPushButton("+ 等待")
        b_seq = QPushButton("+ 动作组")
        b_snap = QPushButton("+ 抓拍")
        mid.addWidget(b_add_pois)
        mid.addWidget(b_home)
        mid.addWidget(b_wait)
        mid.addWidget(b_seq)
        mid.addWidget(b_snap)
        mid.addStretch(1)

        right = QVBoxLayout()
        right.addWidget(QLabel("步骤序列（按顺序）"))
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        right.addWidget(self.list, 1)
        dwell_row = QHBoxLayout()
        dwell_row.addWidget(QLabel("选中步停留:"))
        self.dwell_spin = QSpinBox()
        self.dwell_spin.setRange(0, 3600)
        self.dwell_spin.setSuffix(" 秒")
        self.dwell_spin.setEnabled(False)
        self.dwell_spin.valueChanged.connect(self._on_dwell_changed)
        dwell_row.addWidget(self.dwell_spin)
        dwell_row.addStretch(1)
        right.addLayout(dwell_row)
        edit_row = QHBoxLayout()
        b_edit = QPushButton("编辑")
        b_del = QPushButton("删除")
        b_up = QPushButton("上移")
        b_dn = QPushButton("下移")
        edit_row.addWidget(b_edit)
        edit_row.addWidget(b_del)
        edit_row.addWidget(b_up)
        edit_row.addWidget(b_dn)
        right.addLayout(edit_row)

        lists.addLayout(left, 1)
        lists.addLayout(mid)
        lists.addLayout(right, 1)
        root.addLayout(lists, 1)

        # ---- 定时 ----
        self.cmb_sched = QComboBox()
        self.cmb_sched.addItem("不定时（手动）", ScheduleKind.NONE)
        self.cmb_sched.addItem("每日循环", ScheduleKind.DAILY)
        self.cmb_sched.addItem("单次（日历）", ScheduleKind.ONCE)
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setDate(QDate.currentDate())
        self.time_edit = QTimeEdit()
        self.time_edit.setDisplayFormat("HH:mm")
        self.cmb_sched.currentIndexChanged.connect(self._on_sched_kind)
        sched = QHBoxLayout()
        sched.addWidget(QLabel("定时:"))
        sched.addWidget(self.cmb_sched)
        sched.addWidget(QLabel("日期:"))
        sched.addWidget(self.date_edit)
        sched.addWidget(QLabel("时间:"))
        sched.addWidget(self.time_edit)
        sched.addStretch(1)
        root.addLayout(sched)

        hint = QLabel(
            "导航/回桩可设到点停留；亦可插入等待、动作组、抓拍。"
            " 定时到点后自动执行（需程序在运行且无其它任务组在跑）。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666;font-size:10px;")
        root.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        b_add_pois.clicked.connect(self._add_selected_pois)
        b_home.clicked.connect(lambda: self._append(MissionStep("go_home", {"dwell_s": 0})))
        b_wait.clicked.connect(self._add_wait)
        b_seq.clicked.connect(self._add_sequence)
        b_snap.clicked.connect(lambda: self._append(MissionStep("snapshot", {})))
        b_edit.clicked.connect(self._edit_selected)
        b_del.clicked.connect(self._remove_selected)
        b_up.clicked.connect(lambda: self._move_selected(-1))
        b_dn.clicked.connect(lambda: self._move_selected(1))
        self.list.currentRowChanged.connect(self._on_step_sel)

        self._refresh_list()
        if mission:
            idx = self.cmb_sched.findData(mission.schedule_kind or ScheduleKind.NONE)
            if idx >= 0:
                self.cmb_sched.setCurrentIndex(idx)
            if mission.schedule_time:
                parts = mission.schedule_time.split(" ")
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

    def _refresh_list(self) -> None:
        row = self.list.currentRow()
        self.list.clear()
        for i, s in enumerate(self._steps):
            # 补全星标名显示
            if s.kind == "navigate_poi":
                pid = str(s.params.get("poi_id") or "")
                if not s.params.get("poi_name") and pid in self._poi_name:
                    s.params["poi_name"] = self._poi_name[pid]
            self.list.addItem(f"{i + 1}. {step_summary(s)}")
        if 0 <= row < len(self._steps):
            self.list.setCurrentRow(row)

    def _append(self, step: MissionStep) -> None:
        self._steps.append(step)
        self._refresh_list()
        self.list.setCurrentRow(len(self._steps) - 1)

    def _add_selected_pois(self) -> None:
        selected = self.src.selectedItems()
        if not selected:
            QMessageBox.information(self, "任务组", "请先在左侧选中一个或多个星标。")
            return
        for it in selected:
            pid = str(it.data(256))
            name = self._poi_name.get(pid) or it.text()
            self._steps.append(
                MissionStep("navigate_poi", {"poi_id": pid, "poi_name": name, "dwell_s": 0})
            )
        self._refresh_list()
        self.list.setCurrentRow(len(self._steps) - 1)

    def _add_wait(self) -> None:
        sec, ok = QInputDialog.getDouble(self, "等待", "秒数:", 1.0, 0.1, 3600.0, 1)
        if ok:
            self._append(MissionStep("wait", {"seconds": float(sec)}))

    def _configure_sequence_step(self, initial: Optional[dict] = None) -> Optional[dict]:
        names = list(self.get_sequences() or [])
        if not names:
            QMessageBox.information(self, "任务组", "没有已保存的动作组。")
            return None
        dlg = SequenceStepConfigDialog(
            sequence_names=names,
            get_sequence_poses=self.get_sequence_poses,
            initial=initial,
            parent=self,
        )
        if dlg.exec_() != QDialog.Accepted:
            return None
        return dlg.result_params()

    def _add_sequence(self) -> None:
        params = self._configure_sequence_step()
        if params:
            self._append(MissionStep("run_sequence", params))

    def _on_step_sel(self, row: int) -> None:
        self.dwell_spin.blockSignals(True)
        if row < 0 or row >= len(self._steps):
            self.dwell_spin.setEnabled(False)
            self.dwell_spin.setValue(0)
        else:
            s = self._steps[row]
            can_dwell = s.kind in ("navigate_poi", "go_home")
            self.dwell_spin.setEnabled(can_dwell)
            self.dwell_spin.setValue(int(float(s.params.get("dwell_s") or 0)) if can_dwell else 0)
        self.dwell_spin.blockSignals(False)

    def _on_dwell_changed(self, val: int) -> None:
        row = self.list.currentRow()
        if row < 0 or row >= len(self._steps):
            return
        s = self._steps[row]
        if s.kind not in ("navigate_poi", "go_home"):
            return
        s.params["dwell_s"] = int(val)
        self._refresh_list()

    def _edit_selected(self) -> None:
        row = self.list.currentRow()
        if row < 0 or row >= len(self._steps):
            QMessageBox.information(self, "任务组", "请先选中一步。")
            return
        step = self._steps[row]
        if step.kind == "navigate_poi":
            pois = list(self.get_pois() or [])
            if not pois:
                return
            labels = [f"{n}" for _pid, n in pois]
            cur = str(step.params.get("poi_id") or "")
            ix = next((i for i, (pid, _) in enumerate(pois) if pid == cur), 0)
            label, ok = QInputDialog.getItem(self, "导航星标", "选择星标:", labels, ix, False)
            if ok:
                pid, name = pois[labels.index(label)]
                step.params["poi_id"] = pid
                step.params["poi_name"] = name
                self._refresh_list()
            return
        if step.kind == "wait":
            cur = float(step.params.get("seconds", 1))
            sec, ok = QInputDialog.getDouble(self, "等待", "秒数:", cur, 0.1, 3600.0, 1)
            if ok:
                step.params["seconds"] = float(sec)
                self._refresh_list()
            return
        if step.kind == "run_sequence":
            params = self._configure_sequence_step(dict(step.params))
            if params:
                step.params.clear()
                step.params.update(params)
                self._refresh_list()
            return
        QMessageBox.information(self, "任务组", "该步骤用下方「停留」或无需额外参数。")

    def _remove_selected(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        self._steps.pop(row)
        self._refresh_list()

    def _move_selected(self, delta: int) -> None:
        row = self.list.currentRow()
        nb = row + delta
        if row < 0 or nb < 0 or nb >= len(self._steps):
            return
        self._steps[row], self._steps[nb] = self._steps[nb], self._steps[row]
        self._refresh_list()
        self.list.setCurrentRow(nb)

    def _on_sched_kind(self) -> None:
        kind = self.cmb_sched.currentData()
        timed = kind != ScheduleKind.NONE
        self.time_edit.setEnabled(timed)
        self.date_edit.setEnabled(kind == ScheduleKind.ONCE)

    def _schedule_fields(self) -> tuple[str, str]:
        kind = self.cmb_sched.currentData()
        hhmm = self.time_edit.time().toString("HH:mm")
        if kind == ScheduleKind.NONE:
            return kind, ""
        if kind == ScheduleKind.DAILY:
            return kind, hhmm
        return kind, f"{self.date_edit.date().toString('yyyy-MM-dd')} {hhmm}"

    def _accept(self) -> None:
        name = self.ed_name.text().strip()
        if not name:
            QMessageBox.warning(self, "任务组", "请填写名称。")
            return
        if not self._steps:
            QMessageBox.warning(self, "任务组", "请至少添加一个步骤。")
            return
        self.accept()

    def result_mission(self) -> Mission:
        sk, st = self._schedule_fields()
        last_run = self._keep_last_run if (sk, st) == self._orig_schedule else ""
        return Mission(
            id=self._mission_id,
            name=self.ed_name.text().strip(),
            steps=[MissionStep(s.kind, dict(s.params)) for s in self._steps],
            status="待执行",
            cur_idx=0,
            reason="",
            schedule_kind=sk,
            schedule_time=st,
            last_run_date=last_run,
        )
