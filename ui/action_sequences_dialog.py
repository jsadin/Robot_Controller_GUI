"""动作组编辑器与执行（对齐 ES66 独立版：循环 + 步间延时）。"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional, Sequence

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QVBoxLayout,
)

from devices.arm.sequences import (
    DEFAULT_LOOP_COUNT,
    MAX_LOOP_COUNT,
    MIN_LOOP_COUNT,
    MIN_STEP_DELAY_AFTER_S,
    SequenceEntry,
    default_poses_path,
    default_sequences_path,
    load_poses,
    load_sequences,
    save_poses,
    save_sequences,
)


class ActionSequencesDialog(QDialog):
    """位姿库 + 多步动作组（循环 / 步间延时 / 可停止）。"""

    def __init__(
        self,
        data_dir: Path,
        get_current_joints: Callable[[], Optional[Sequence[float]]],
        run_pose: Callable[[Sequence[float]], None],
        is_arm_connected: Optional[Callable[[], bool]] = None,
        on_status: Optional[Callable[[str], None]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("动作组 / 位姿库")
        self.setModal(False)
        self.resize(640, 560)

        self.data_dir = data_dir
        self.get_current_joints = get_current_joints
        self.run_pose = run_pose
        self.is_arm_connected = is_arm_connected or (lambda: True)
        self.on_status = on_status or (lambda _m: None)

        self.poses_path = default_poses_path(data_dir)
        self.seq_path = default_sequences_path(data_dir)
        self.poses = load_poses(self.poses_path)
        self.seqs: dict[str, SequenceEntry] = load_sequences(self.seq_path)

        root = QVBoxLayout(self)

        # ---- 位姿库 ----
        root.addWidget(QLabel("位姿库"))
        self.list_poses = QListWidget()
        self.list_poses.setMaximumHeight(120)
        root.addWidget(self.list_poses)
        pose_row = QHBoxLayout()
        b_save = QPushButton("记录当前")
        b_goto = QPushButton("前往选中")
        b_del = QPushButton("删除位姿")
        pose_row.addWidget(b_save)
        pose_row.addWidget(b_goto)
        pose_row.addWidget(b_del)
        pose_row.addStretch(1)
        root.addLayout(pose_row)

        # ---- 动作组 ----
        seq_top = QHBoxLayout()
        seq_top.addWidget(QLabel("当前组"))
        self._cmb_action_group = QComboBox()
        self._cmb_action_group.setMinimumWidth(160)
        self._cmb_action_group.currentIndexChanged.connect(self._on_action_group_pick_changed)
        seq_top.addWidget(self._cmb_action_group)
        self._btn_seq_new = QPushButton("新建组")
        self._btn_seq_new.clicked.connect(self._on_action_group_new)
        seq_top.addWidget(self._btn_seq_new)
        self._btn_seq_save = QPushButton("保存到磁盘")
        self._btn_seq_save.setToolTip("将当前表格与循环选项写入该组，并保存 JSON")
        self._btn_seq_save.clicked.connect(self._on_action_group_save_file)
        seq_top.addWidget(self._btn_seq_save)
        self._btn_seq_del = QPushButton("删除组")
        self._btn_seq_del.clicked.connect(self._on_action_group_delete)
        seq_top.addWidget(self._btn_seq_del)
        seq_top.addStretch(1)
        root.addLayout(seq_top)

        loop_row = QHBoxLayout()
        self._chk_seq_loop = QCheckBox("循环执行")
        self._chk_seq_loop.setToolTip("勾选后按「循环次数」完整跑完整组；最后一步延时结束后回到第一步")
        self._chk_seq_loop.toggled.connect(self._on_loop_toggled)
        loop_row.addWidget(self._chk_seq_loop)
        loop_row.addWidget(QLabel("循环次数"))
        self._spin_loop_count = QSpinBox()
        self._spin_loop_count.setRange(MIN_LOOP_COUNT, MAX_LOOP_COUNT)
        self._spin_loop_count.setValue(DEFAULT_LOOP_COUNT)
        self._spin_loop_count.setSuffix(" 次")
        self._spin_loop_count.setToolTip(f"整组完整执行次数，默认 {DEFAULT_LOOP_COUNT} 次")
        self._spin_loop_count.setEnabled(False)
        loop_row.addWidget(self._spin_loop_count)
        loop_row.addStretch(1)
        root.addLayout(loop_row)

        self._tbl_sequences = QTableWidget(0, 2)
        self._tbl_sequences.setHorizontalHeaderLabels(["点位名称", "本步后到下一步前延时"])
        hh = self._tbl_sequences.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._tbl_sequences.verticalHeader().setVisible(False)
        self._tbl_sequences.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._tbl_sequences.setMinimumHeight(140)
        root.addWidget(self._tbl_sequences)

        seq_btn_row = QHBoxLayout()
        self._btn_seq_ins = QPushButton("+ 插入步")
        self._btn_seq_ins.clicked.connect(self._on_action_step_insert)
        self._btn_seq_rm = QPushButton("− 删除步")
        self._btn_seq_rm.clicked.connect(self._on_action_step_remove)
        self._btn_seq_up = QPushButton("上移")
        self._btn_seq_up.clicked.connect(lambda: self._on_action_step_move(-1))
        self._btn_seq_dn = QPushButton("下移")
        self._btn_seq_dn.clicked.connect(lambda: self._on_action_step_move(1))
        seq_btn_row.addWidget(self._btn_seq_ins)
        seq_btn_row.addWidget(self._btn_seq_rm)
        seq_btn_row.addWidget(self._btn_seq_up)
        seq_btn_row.addWidget(self._btn_seq_dn)
        seq_btn_row.addStretch(1)
        self._btn_seq_start = QPushButton("▶ 开始动作组")
        self._btn_seq_start.clicked.connect(self._on_action_sequence_start)
        seq_btn_row.addWidget(self._btn_seq_start)
        self._btn_seq_stop = QPushButton("■ 停止")
        self._btn_seq_stop.setEnabled(False)
        self._btn_seq_stop.clicked.connect(self.force_stop_runner)
        seq_btn_row.addWidget(self._btn_seq_stop)
        root.addLayout(seq_btn_row)

        hint = QLabel(
            "每一步从「位姿库」中选一个点位；「本步后到下一步前延时」默认 "
            f"{MIN_STEP_DELAY_AFTER_S:g} 秒，最小亦为 {MIN_STEP_DELAY_AFTER_S:g} 秒。"
            f" 勾选循环后可设整组执行次数（默认 {DEFAULT_LOOP_COUNT} 次）。"
            " 关闭此窗口不会停止已开始运行的序列（点 ■ 停止 或主界面急停会停止）。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666;font-size:10px;")
        root.addWidget(hint)

        lbl_path = QLabel(f"保存文件：{self.seq_path}")
        lbl_path.setWordWrap(True)
        lbl_path.setStyleSheet("color:#555;font-size:10px;")
        root.addWidget(lbl_path)

        self._seq_pick_prev_name: Optional[str] = None
        self._runner_active = False
        self._runner_steps: list[tuple[str, float]] = []
        self._runner_i = -1
        self._runner_loop = False
        self._runner_loop_count = 1
        self._runner_pass = 1  # 当前第几遍（从 1 起）
        self._runner_timer = QTimer(self)
        self._runner_timer.setSingleShot(True)
        self._runner_timer.timeout.connect(self._seq_runner_after_delay_elapsed)

        b_save.clicked.connect(self._save_pose)
        b_goto.clicked.connect(self._goto_pose)
        b_del.clicked.connect(self._del_pose)

        self._refresh_poses_list()
        self._action_group_refresh_combo()

    # ---- public API ----

    def force_stop_runner(self, *, log_stop: bool = True) -> None:
        was = self._runner_active
        self._runner_active = False
        self._runner_timer.stop()
        self._set_action_sequence_running_ui(False)
        if was and log_stop:
            self.on_status("动作组顺序执行已停止")

    def is_runner_active(self) -> bool:
        return bool(self._runner_active)

    def sync_and_save(self) -> None:
        self._sync_current_action_group_from_ui_into_dict()
        self._persist()

    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        self.poses = load_poses(self.poses_path)
        self._refresh_poses_list()
        self._refresh_action_sequence_pose_combos()
        self.raise_()

    def closeEvent(self, event) -> None:  # noqa: ANN001
        # 关闭窗口不停止 runner（对象由主窗口持有，与原版一致）
        self.sync_and_save()
        super().closeEvent(event)

    # ---- pose library ----

    def _refresh_poses_list(self) -> None:
        self.list_poses.clear()
        for n in sorted(self.poses.keys()):
            self.list_poses.addItem(n)

    def _pose_names(self) -> list[str]:
        return sorted(self.poses.keys())

    def _save_pose(self) -> None:
        name, ok = QInputDialog.getText(self, "位姿名", "名称:")
        if not ok or not name.strip():
            return
        j = self.get_current_joints()
        if not j or len(j) != 6:
            QMessageBox.warning(self, "位姿", "无当前关节角")
            return
        self.poses[name.strip()] = [float(x) for x in j]
        save_poses(self.poses_path, self.poses)
        self._refresh_poses_list()
        self._refresh_action_sequence_pose_combos()
        self.on_status(f"已记录位姿：{name.strip()}")

    def _goto_pose(self) -> None:
        item = self.list_poses.currentItem()
        if not item:
            return
        name = item.text()
        if name not in self.poses:
            return
        if not self.is_arm_connected():
            QMessageBox.information(self, "位姿", "请先连接机械臂。")
            return
        self.run_pose(self.poses[name])

    def _del_pose(self) -> None:
        item = self.list_poses.currentItem()
        if not item:
            return
        self.poses.pop(item.text(), None)
        save_poses(self.poses_path, self.poses)
        self._refresh_poses_list()
        self._refresh_action_sequence_pose_combos()

    # ---- persist / sync ----

    def _persist(self) -> None:
        try:
            save_sequences(self.seq_path, self.seqs)
        except OSError as e:
            self.on_status(f"保存动作组失败：{e}")
            QMessageBox.warning(self, "动作组", f"保存失败：{e}")

    def _sync_current_action_group_from_ui_into_dict(self) -> None:
        name = self._cmb_action_group.currentText().strip()
        if name and name in self.seqs:
            self.seqs[name] = self._action_group_gather_from_ui()

    def _on_loop_toggled(self, checked: bool) -> None:
        self._spin_loop_count.setEnabled(bool(checked))

    def _action_group_gather_from_ui(self) -> SequenceEntry:
        loop = self._chk_seq_loop.isChecked()
        loop_count = int(self._spin_loop_count.value())
        steps: list[tuple[str, float]] = []
        for r in range(self._tbl_sequences.rowCount()):
            cw0 = self._tbl_sequences.cellWidget(r, 0)
            cw1 = self._tbl_sequences.cellWidget(r, 1)
            if not isinstance(cw0, QComboBox) or not isinstance(cw1, QDoubleSpinBox):
                continue
            pname = cw0.currentText().strip()
            if not pname:
                continue
            steps.append((pname, max(MIN_STEP_DELAY_AFTER_S, float(cw1.value()))))
        return loop, loop_count, steps

    def _on_step_delay_spin_changed(self, spin: QDoubleSpinBox, value: float) -> None:
        if value >= MIN_STEP_DELAY_AFTER_S:
            return
        spin.blockSignals(True)
        spin.setValue(float(MIN_STEP_DELAY_AFTER_S))
        spin.blockSignals(False)
        QMessageBox.warning(
            self,
            "动作组",
            f"单步延时不得小于 {MIN_STEP_DELAY_AFTER_S:g} 秒（当前值已改为 {MIN_STEP_DELAY_AFTER_S:g} 秒）。",
        )

    def _insert_sequence_step_row_at(self, row: int, pose_name: str, delay_after_s: float) -> None:
        names = self._pose_names()
        if not names:
            return
        self._tbl_sequences.insertRow(row)
        cb = QComboBox()
        for n in names:
            cb.addItem(n)
        ix = cb.findText(pose_name)
        cb.setCurrentIndex(ix if ix >= 0 else 0)
        sp = QDoubleSpinBox()
        sp.blockSignals(True)
        sp.setRange(0.0, 3600.0)
        sp.setDecimals(2)
        sp.setSuffix(" s")
        sp.setSingleStep(0.5)
        sp.setKeyboardTracking(False)
        clamped = max(MIN_STEP_DELAY_AFTER_S, min(3600.0, float(delay_after_s)))
        sp.setValue(clamped)
        sp.blockSignals(False)
        sp.valueChanged.connect(lambda v, s=sp: self._on_step_delay_spin_changed(s, float(v)))
        self._tbl_sequences.setCellWidget(row, 0, cb)
        self._tbl_sequences.setCellWidget(row, 1, sp)

    def _refresh_action_sequence_pose_combos(self) -> None:
        names = self._pose_names()
        for r in range(self._tbl_sequences.rowCount()):
            w = self._tbl_sequences.cellWidget(r, 0)
            if not isinstance(w, QComboBox):
                continue
            sel = w.currentText()
            w.blockSignals(True)
            w.clear()
            for n in names:
                w.addItem(n)
            ix = w.findText(sel)
            if ix >= 0:
                w.setCurrentIndex(ix)
            elif w.count() > 0:
                w.setCurrentIndex(0)
            w.blockSignals(False)

    def _action_group_clear_table(self) -> None:
        self._tbl_sequences.setRowCount(0)

    def _action_group_load_named_into_ui(self, name: Optional[str]) -> None:
        self._action_group_clear_table()
        self._chk_seq_loop.blockSignals(True)
        self._chk_seq_loop.setChecked(False)
        self._chk_seq_loop.blockSignals(False)
        self._spin_loop_count.setValue(DEFAULT_LOOP_COUNT)
        self._spin_loop_count.setEnabled(False)
        if not name or name not in self.seqs:
            return
        loop, loop_count, steps = self.seqs[name]
        self._chk_seq_loop.blockSignals(True)
        self._chk_seq_loop.setChecked(loop)
        self._chk_seq_loop.blockSignals(False)
        self._spin_loop_count.setValue(loop_count)
        self._spin_loop_count.setEnabled(loop)
        for pname, delay in steps:
            r = self._tbl_sequences.rowCount()
            self._insert_sequence_step_row_at(r, pname, delay)

    def _action_group_refresh_combo(self, select: Optional[str] = None) -> None:
        self._cmb_action_group.blockSignals(True)
        old_cur = self._cmb_action_group.currentText().strip()
        if old_cur and old_cur in self.seqs:
            self.seqs[old_cur] = self._action_group_gather_from_ui()

        preserve = select if select is not None else old_cur
        self._cmb_action_group.clear()
        for key in sorted(self.seqs.keys()):
            self._cmb_action_group.addItem(key)

        if self._cmb_action_group.count() == 0:
            self._seq_pick_prev_name = None
            self._cmb_action_group.blockSignals(False)
            self._action_group_load_named_into_ui(None)
            return

        ix = self._cmb_action_group.findText(preserve)
        if preserve and ix >= 0:
            self._cmb_action_group.setCurrentIndex(ix)
        else:
            self._cmb_action_group.setCurrentIndex(0)

        self._seq_pick_prev_name = self._cmb_action_group.currentText().strip() or None
        self._cmb_action_group.blockSignals(False)
        self._action_group_load_named_into_ui(self._seq_pick_prev_name)

    def _on_action_group_pick_changed(self, _index: int) -> None:
        prev = self._seq_pick_prev_name
        cur = self._cmb_action_group.currentText().strip()
        if prev and prev != cur and prev in self.seqs:
            self.seqs[prev] = self._action_group_gather_from_ui()
        self._seq_pick_prev_name = cur or None
        self._action_group_load_named_into_ui(cur if cur and cur in self.seqs else None)

    def _on_action_group_new(self) -> None:
        self._sync_current_action_group_from_ui_into_dict()
        n = max(1, len(self.seqs) + 1)
        nm = f"动作组{n}"
        while nm in self.seqs:
            n += 1
            nm = f"动作组{n}"
        self.seqs[nm] = (False, DEFAULT_LOOP_COUNT, [])
        self._persist()
        self._action_group_refresh_combo(select=nm)

    def _on_action_group_save_file(self) -> None:
        name = self._cmb_action_group.currentText().strip()
        if not name:
            QMessageBox.information(self, "动作组", "请先选择或新建一个动作组后再保存。")
            return
        entry = self._action_group_gather_from_ui()
        self.seqs[name] = entry
        self._persist()
        QMessageBox.information(self, "动作组", f"已保存：{name}\n路径：{self.seq_path}")

    def _on_action_group_delete(self) -> None:
        name = self._cmb_action_group.currentText().strip()
        if not name or name not in self.seqs:
            QMessageBox.information(self, "动作组", "没有可删除的组。")
            return
        if (
            QMessageBox.question(
                self,
                "动作组",
                f"删除动作组「{name}」？",
                QMessageBox.Yes | QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return
        del self.seqs[name]
        self._seq_pick_prev_name = None
        self._persist()
        self._action_group_refresh_combo()

    def _on_action_step_insert(self) -> None:
        if not self._pose_names():
            QMessageBox.information(self, "动作组", "请先在「位姿库」中至少记录一个点位。")
            return
        r_cur = self._tbl_sequences.currentRow()
        ins = max(0, self._tbl_sequences.rowCount()) if r_cur < 0 else r_cur + 1
        default_pose = self._pose_names()[0]
        self._insert_sequence_step_row_at(ins, default_pose, MIN_STEP_DELAY_AFTER_S)
        self._tbl_sequences.setCurrentCell(ins, 0)

    def _on_action_step_remove(self) -> None:
        r_cur = self._tbl_sequences.currentRow()
        if r_cur < 0:
            QMessageBox.information(self, "动作组", "请先选中表格中的一步。")
            return
        self._tbl_sequences.removeRow(r_cur)

    def _swap_sequence_table_rows(self, a: int, b: int) -> None:
        n = self._tbl_sequences.rowCount()
        if a < 0 or b < 0 or a >= n or b >= n or a == b:
            return
        for c in range(2):
            wa = self._tbl_sequences.cellWidget(a, c)
            wb = self._tbl_sequences.cellWidget(b, c)
            self._tbl_sequences.removeCellWidget(a, c)
            self._tbl_sequences.removeCellWidget(b, c)
            self._tbl_sequences.setCellWidget(a, c, wb)
            self._tbl_sequences.setCellWidget(b, c, wa)
        self._tbl_sequences.selectRow(b)

    def _on_action_step_move(self, delta: int) -> None:
        r_cur = self._tbl_sequences.currentRow()
        if r_cur < 0:
            QMessageBox.information(self, "动作组", "请先选中表格中的一步。")
            return
        self._swap_sequence_table_rows(r_cur, r_cur + delta)

    def _set_action_sequence_running_ui(self, running: bool) -> None:
        self._btn_seq_start.setEnabled(not running)
        self._btn_seq_stop.setEnabled(running)
        self._tbl_sequences.setEnabled(not running)
        self._chk_seq_loop.setEnabled(not running)
        self._spin_loop_count.setEnabled(not running and self._chk_seq_loop.isChecked())
        self._cmb_action_group.setEnabled(not running)
        self._btn_seq_new.setEnabled(not running)
        self._btn_seq_save.setEnabled(not running)
        self._btn_seq_del.setEnabled(not running)
        self._btn_seq_ins.setEnabled(not running)
        self._btn_seq_rm.setEnabled(not running)
        self._btn_seq_up.setEnabled(not running)
        self._btn_seq_dn.setEnabled(not running)
        self.list_poses.setEnabled(not running)

    def _seq_runner_after_delay_elapsed(self) -> None:
        if not self._runner_active:
            return
        n = len(self._runner_steps)
        if n == 0:
            self.force_stop_runner(log_stop=False)
            return

        self._runner_i += 1
        if self._runner_i >= n:
            if self._runner_loop and self._runner_pass < self._runner_loop_count:
                self._runner_pass += 1
                self._runner_i = 0
            else:
                self.force_stop_runner(log_stop=False)
                if self._runner_loop:
                    self.on_status(
                        f"动作组已完成：共循环 {self._runner_loop_count} 次"
                    )
                else:
                    self.on_status("动作组单次序列已执行完毕")
                return

        pname, delay = self._runner_steps[self._runner_i]
        deg = self.poses.get(pname)
        if deg is None:
            self.on_status(f"动作组中止：点位「{pname}」不存在")
            QMessageBox.warning(self, "动作组", f"点位「{pname}」不存在，动作组已停止。")
            self.force_stop_runner(log_stop=False)
            return
        self.run_pose(deg)
        pass_info = ""
        if self._runner_loop:
            pass_info = f"，第 {self._runner_pass}/{self._runner_loop_count} 遍"
        self.on_status(
            f"动作组步骤 {self._runner_i + 1}/{n}：{pname}，延时 {delay:g}s{pass_info}"
        )
        ms = max(0, int(round(delay * 1000.0)))
        self._runner_timer.start(ms)

    def _on_action_sequence_start(self) -> None:
        if self._runner_active:
            return
        if not self.is_arm_connected():
            QMessageBox.information(self, "动作组", "请先连接机械臂后再执行动作组。")
            return

        self._sync_current_action_group_from_ui_into_dict()
        nm = self._cmb_action_group.currentText().strip()
        if not nm or nm not in self.seqs:
            QMessageBox.information(self, "动作组", "请选择一个有效的动作组。")
            return

        loop, loop_count, steps = self._action_group_gather_from_ui()
        self.seqs[nm] = (loop, loop_count, steps)
        self._persist()

        if not steps:
            QMessageBox.information(self, "动作组", "请至少包含一步有效的点位。")
            return

        for pname, _d in steps:
            if pname not in self.poses:
                QMessageBox.warning(
                    self, "动作组", f"表中点位「{pname}」在位姿库中不存在。\n请先记录或更正该步。"
                )
                return

        self._runner_steps = [(p, max(MIN_STEP_DELAY_AFTER_S, float(d))) for p, d in steps]
        self._runner_loop = loop
        self._runner_loop_count = loop_count if loop else 1
        self._runner_pass = 1
        self._runner_i = -1
        self._runner_active = True
        self._set_action_sequence_running_ui(True)
        if loop:
            self.on_status(
                f"动作组「{nm}」开始：共 {len(steps)} 步，循环 {loop_count} 次"
            )
        else:
            self.on_status(f"动作组「{nm}」开始：共 {len(steps)} 步，单次执行")
        try:
            self._seq_runner_after_delay_elapsed()
        except Exception as exc:
            self.force_stop_runner(log_stop=False)
            self.on_status(f"动作组执行异常：{exc!r}")
            QMessageBox.warning(self, "动作组", "启动动作组时出现异常。")
