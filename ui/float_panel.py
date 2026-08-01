"""浮动面板：关闭或点「停靠」时把内容交还 MainWorkspace。"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class FloatPanel(QDialog):
    def __init__(
        self,
        slot_id: str,
        title: str,
        content: QWidget,
        on_dock: Callable[[str, QWidget], None],
        parent=None,
    ):
        super().__init__(parent, Qt.Window)
        self.slot_id = slot_id
        self._on_dock = on_dock
        self._content: Optional[QWidget] = content
        self._docking = False

        self.setWindowTitle(title)
        self.setModal(False)
        self.resize(480, 420)
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        head = QHBoxLayout()
        head.addWidget(QLabel(title), 1)
        self.btn_dock = QPushButton("停靠")
        self.btn_dock.setObjectName("primary")
        head.addWidget(self.btn_dock)

        self._body = QVBoxLayout()
        self._body.setContentsMargins(0, 0, 0, 0)
        if content is not None:
            self._body.addWidget(content, 1)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.addLayout(head)
        root.addLayout(self._body, 1)

        self.btn_dock.clicked.connect(self._dock)

    def _dock(self) -> None:
        self._docking = True
        w = self._content
        self._content = None
        if w is not None:
            self._body.removeWidget(w)
            w.setParent(None)
            self._on_dock(self.slot_id, w)
        self.close()

    def closeEvent(self, event) -> None:
        if not self._docking and self._content is not None:
            w = self._content
            self._content = None
            self._body.removeWidget(w)
            w.setParent(None)
            self._on_dock(self.slot_id, w)
        super().closeEvent(event)
