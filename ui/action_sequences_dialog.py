"""动作组简易对话框（ArmActionSequence）。"""

from __future__ import annotations

from pathlib import Path

from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from devices.arm.sequences import (
    default_poses_path,
    default_sequences_path,
    load_poses,
    load_sequences,
    save_poses,
    save_sequences,
)


class ActionSequencesDialog(QDialog):
    def __init__(self, data_dir: Path, get_current_joints, run_pose, parent=None):
        super().__init__(parent)
        self.setWindowTitle("动作组 / 位姿库")
        self.resize(420, 360)
        self.data_dir = data_dir
        self.get_current_joints = get_current_joints
        self.run_pose = run_pose
        self.poses_path = default_poses_path(data_dir)
        self.seq_path = default_sequences_path(data_dir)
        self.poses = load_poses(self.poses_path)
        self.seqs = load_sequences(self.seq_path)

        root = QVBoxLayout(self)
        root.addWidget(QLabel("位姿库"))
        self.list_poses = QListWidget()
        root.addWidget(self.list_poses)
        row = QHBoxLayout()
        b_save = QPushButton("记录当前")
        b_goto = QPushButton("前往选中")
        b_del = QPushButton("删除位姿")
        row.addWidget(b_save)
        row.addWidget(b_goto)
        row.addWidget(b_del)
        root.addLayout(row)

        root.addWidget(QLabel("动作组（选中位姿加入）"))
        self.list_seq = QListWidget()
        root.addWidget(self.list_seq)
        row2 = QHBoxLayout()
        b_add = QPushButton("加入选中位姿")
        b_run = QPushButton("运行动作组")
        b_new = QPushButton("新建动作组")
        row2.addWidget(b_new)
        row2.addWidget(b_add)
        row2.addWidget(b_run)
        root.addLayout(row2)

        b_save.clicked.connect(self._save_pose)
        b_goto.clicked.connect(self._goto_pose)
        b_del.clicked.connect(self._del_pose)
        b_new.clicked.connect(self._new_seq)
        b_add.clicked.connect(self._add_step)
        b_run.clicked.connect(self._run_seq)
        self._refresh()

    def _refresh(self) -> None:
        self.list_poses.clear()
        for n in sorted(self.poses.keys()):
            self.list_poses.addItem(n)
        self.list_seq.clear()
        for n in sorted(self.seqs.keys()):
            loop, steps = self.seqs[n]
            self.list_seq.addItem(f"{n} ({len(steps)}步{' 循环' if loop else ''})")

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
        self._refresh()

    def _goto_pose(self) -> None:
        item = self.list_poses.currentItem()
        if not item:
            return
        name = item.text()
        self.run_pose(self.poses[name])

    def _del_pose(self) -> None:
        item = self.list_poses.currentItem()
        if not item:
            return
        self.poses.pop(item.text(), None)
        save_poses(self.poses_path, self.poses)
        self._refresh()

    def _new_seq(self) -> None:
        name, ok = QInputDialog.getText(self, "动作组", "名称:")
        if not ok or not name.strip():
            return
        self.seqs[name.strip()] = (False, [])
        save_sequences(self.seq_path, self.seqs)
        self._refresh()

    def _add_step(self) -> None:
        pitem = self.list_poses.currentItem()
        sitem = self.list_seq.currentItem()
        if not pitem or not sitem:
            QMessageBox.information(self, "动作组", "请同时选中位姿与动作组")
            return
        sname = sitem.text().split(" (", 1)[0]
        loop, steps = self.seqs[sname]
        steps = list(steps) + [(pitem.text(), 5.0)]
        self.seqs[sname] = (loop, steps)
        save_sequences(self.seq_path, self.seqs)
        self._refresh()

    def _run_seq(self) -> None:
        sitem = self.list_seq.currentItem()
        if not sitem:
            return
        sname = sitem.text().split(" (", 1)[0]
        _loop, steps = self.seqs.get(sname, (False, []))
        for pose_name, _delay in steps:
            if pose_name in self.poses:
                self.run_pose(self.poses[pose_name])
