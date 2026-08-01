"""分屏面板宿主：标题栏 + 放大 / 还原 / 弹出 / 移到。"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

# 槽位名
SLOT_MAP = "map"
SLOT_CHASSIS = "chassis"
SLOT_ARM = "arm"
SLOT_VISION = "vision"
ALL_SLOTS = (SLOT_MAP, SLOT_CHASSIS, SLOT_ARM, SLOT_VISION)

SLOT_LABELS = {
    SLOT_MAP: "地图区",
    SLOT_CHASSIS: "左下(遥控)",
    SLOT_ARM: "中下(机械臂)",
    SLOT_VISION: "右下(视觉)",
}


class PanelHost(QFrame):
    """承载一个内容控件，向外发布局操作信号（由 MainWorkspace 处理）。"""

    maximizeRequested = pyqtSignal(str)   # slot_id
    restoreRequested = pyqtSignal()
    popOutRequested = pyqtSignal(str)     # slot_id
    moveToRequested = pyqtSignal(str, str)  # from_slot, to_slot

    def __init__(self, slot_id: str, title: str, content: QWidget, parent=None):
        super().__init__(parent)
        self.slot_id = slot_id
        self.setObjectName("PanelHost")
        self.setFrameShape(QFrame.StyledPanel)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self._title = title
        self._content: Optional[QWidget] = None
        self._maximized = False

        self.title_label = QLabel(title)
        self.title_label.setObjectName("PanelHostTitle")

        self.btn_max = QPushButton("放大")
        self.btn_max.setFixedHeight(24)
        self.btn_restore = QPushButton("还原")
        self.btn_restore.setFixedHeight(24)
        self.btn_restore.setVisible(False)
        self.btn_pop = QPushButton("弹出")
        self.btn_pop.setFixedHeight(24)
        self.btn_move = QPushButton("移到…")
        self.btn_move.setFixedHeight(24)

        for b in (self.btn_max, self.btn_restore, self.btn_pop, self.btn_move):
            b.setObjectName("PanelHostBtn")

        head = QHBoxLayout()
        head.setContentsMargins(6, 4, 6, 4)
        head.setSpacing(4)
        head.addWidget(self.title_label, 1)
        head.addWidget(self.btn_max)
        head.addWidget(self.btn_restore)
        head.addWidget(self.btn_pop)
        head.addWidget(self.btn_move)

        self._body = QVBoxLayout()
        self._body.setContentsMargins(0, 0, 0, 0)
        self._body.setSpacing(0)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addLayout(head)
        root.addLayout(self._body, 1)

        self.set_content(content)

        self.btn_max.clicked.connect(lambda: self.maximizeRequested.emit(self.slot_id))
        self.btn_restore.clicked.connect(self.restoreRequested.emit)
        self.btn_pop.clicked.connect(lambda: self.popOutRequested.emit(self.slot_id))
        self.btn_move.clicked.connect(self._show_move_menu)

    def content(self) -> Optional[QWidget]:
        return self._content

    def set_content(self, widget: Optional[QWidget]) -> None:
        if self._content is not None:
            self._body.removeWidget(self._content)
            self._content.setParent(None)
            self._content = None
        if widget is not None:
            self._content = widget
            self._body.addWidget(widget, 1)
            widget.show()

    def take_content(self) -> Optional[QWidget]:
        w = self._content
        self.set_content(None)
        return w

    def set_maximized_chrome(self, maximized: bool) -> None:
        self._maximized = maximized
        self.btn_max.setVisible(not maximized)
        self.btn_restore.setVisible(maximized)
        self.btn_pop.setEnabled(not maximized)
        self.btn_move.setEnabled(not maximized)

    def set_empty_placeholder(self, text: str = "（已弹出）") -> None:
        from PyQt5.QtCore import Qt

        ph = QLabel(text)
        ph.setObjectName("PanelHostPlaceholder")
        ph.setAlignment(Qt.AlignCenter)
        self.set_content(ph)

    def _show_move_menu(self) -> None:
        menu = QMenu(self)
        for sid in ALL_SLOTS:
            if sid == self.slot_id:
                continue
            act = menu.addAction(SLOT_LABELS.get(sid, sid))
            act.setData(sid)
        chosen = menu.exec_(self.btn_move.mapToGlobal(self.btn_move.rect().bottomLeft()))
        if chosen is not None:
            to_slot = chosen.data()
            if to_slot:
                self.moveToRequested.emit(self.slot_id, str(to_slot))
