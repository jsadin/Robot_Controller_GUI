"""主区分屏工作区：总览 splitter、放大、弹出、槽位互换。"""

from __future__ import annotations

from typing import Dict, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QLabel, QSplitter, QVBoxLayout, QWidget

from ui.float_panel import FloatPanel
from ui.panel_host import (
    ALL_SLOTS,
    SLOT_ARM,
    SLOT_CHASSIS,
    SLOT_MAP,
    SLOT_VISION,
    PanelHost,
)


class MainWorkspace(QWidget):
    """四槽位：map / chassis / arm / vision。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hosts: Dict[str, PanelHost] = {}
        self._contents: Dict[str, QWidget] = {}  # logical content per slot_id
        self._titles: Dict[str, str] = {
            SLOT_MAP: "地图",
            SLOT_CHASSIS: "底盘遥控",
            SLOT_ARM: "机械臂",
            SLOT_VISION: "视觉",
        }
        self._floats: Dict[str, FloatPanel] = {}
        self._maximized: Optional[str] = None

        # 占位，set_panels 后再填
        for sid, title in self._titles.items():
            host = PanelHost(sid, title, QLabel(""))
            host.maximizeRequested.connect(self.maximize_slot)
            host.restoreRequested.connect(self.restore_overview)
            host.popOutRequested.connect(self.pop_out_slot)
            host.moveToRequested.connect(self.move_slot)
            self._hosts[sid] = host

        self.v_split = QSplitter(Qt.Vertical)
        self.h_split = QSplitter(Qt.Horizontal)
        self.h_split.addWidget(self._hosts[SLOT_CHASSIS])
        self.h_split.addWidget(self._hosts[SLOT_ARM])
        self.h_split.addWidget(self._hosts[SLOT_VISION])

        self.v_split.addWidget(self._hosts[SLOT_MAP])
        self.v_split.addWidget(self.h_split)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.v_split)

        self._max_stack = QWidget()
        self._max_layout = QVBoxLayout(self._max_stack)
        self._max_layout.setContentsMargins(0, 0, 0, 0)
        self._max_stack.hide()
        lay.addWidget(self._max_stack)

    def set_panels(
        self,
        *,
        map_widget: QWidget,
        chassis: QWidget,
        arm: QWidget,
        vision: QWidget,
    ) -> None:
        self._contents = {
            SLOT_MAP: map_widget,
            SLOT_CHASSIS: chassis,
            SLOT_ARM: arm,
            SLOT_VISION: vision,
        }
        for sid, w in self._contents.items():
            self._hosts[sid].set_content(w)
        self.restore_overview()

    def host(self, slot_id: str) -> PanelHost:
        return self._hosts[slot_id]

    def restore_overview(self) -> None:
        """恢复总分屏比例与停靠态（不关闭已弹出的浮动窗）。"""
        if self._maximized is not None:
            sid = self._maximized
            self._maximized = None
            host = self._hosts[sid]
            self._max_layout.removeWidget(host)
            host.setParent(None)
            if sid == SLOT_MAP:
                self.v_split.insertWidget(0, host)
            else:
                order = [SLOT_CHASSIS, SLOT_ARM, SLOT_VISION]
                idx = order.index(sid)
                self.h_split.insertWidget(idx, host)
            host.set_maximized_chrome(False)
            self._max_stack.hide()

        for host in self._hosts.values():
            host.set_maximized_chrome(False)
            host.show()

        self.v_split.show()
        self.v_split.setSizes([700, 300])
        self.h_split.setSizes([100, 100, 120])

    def maximize_slot(self, slot_id: str) -> None:
        if slot_id not in self._hosts:
            return
        if slot_id in self._floats:
            return
        if self._maximized == slot_id:
            return
        if self._maximized is not None:
            self.restore_overview()

        host = self._hosts[slot_id]
        # 从 splitter 取出
        host.setParent(None)
        self._max_layout.addWidget(host)
        host.set_maximized_chrome(True)
        self._maximized = slot_id
        self.v_split.hide()
        self._max_stack.show()

    def maximize_map(self) -> None:
        self.maximize_slot(SLOT_MAP)

    def pop_out_slot(self, slot_id: str) -> None:
        if slot_id in self._floats:
            self._floats[slot_id].raise_()
            self._floats[slot_id].activateWindow()
            return
        if self._maximized == slot_id:
            self.restore_overview()

        host = self._hosts[slot_id]
        content = host.take_content()
        if content is None:
            return
        host.set_empty_placeholder("（已弹出 — 关闭浮窗或点停靠可回槽）")

        dlg = FloatPanel(
            slot_id,
            self._titles.get(slot_id, slot_id),
            content,
            on_dock=self._on_float_dock,
            parent=self.window(),
        )
        self._floats[slot_id] = dlg
        dlg.finished.connect(lambda _r, s=slot_id: self._floats.pop(s, None))
        dlg.show()

    def _on_float_dock(self, slot_id: str, content: QWidget) -> None:
        self._floats.pop(slot_id, None)
        self._contents[slot_id] = content
        host = self._hosts[slot_id]
        # 清掉占位
        host.take_content()
        host.set_content(content)
        host.show()

    def move_slot(self, from_slot: str, to_slot: str) -> None:
        if from_slot == to_slot:
            return
        if from_slot in self._floats or to_slot in self._floats:
            return
        if self._maximized is not None:
            self.restore_overview()

        a = self._hosts[from_slot].take_content()
        b = self._hosts[to_slot].take_content()
        # 交换逻辑内容引用
        ca = self._contents.get(from_slot)
        cb = self._contents.get(to_slot)
        # a/b 应与 contents 一致；用实际 take 到的 widget
        self._contents[from_slot] = b if b is not None else cb
        self._contents[to_slot] = a if a is not None else ca

        if b is not None:
            self._hosts[from_slot].set_content(b)
        else:
            self._hosts[from_slot].set_empty_placeholder("（空）")
        if a is not None:
            self._hosts[to_slot].set_content(a)
        else:
            self._hosts[to_slot].set_empty_placeholder("（空）")

        # 标题随内容类型走：根据 widget 类型不方便，改为交换标题显示
        ta = self._titles[from_slot]
        tb = self._titles[to_slot]
        self._titles[from_slot] = tb
        self._titles[to_slot] = ta
        self._hosts[from_slot].title_label.setText(tb)
        self._hosts[to_slot].title_label.setText(ta)

    def close_all_floats(self) -> None:
        for dlg in list(self._floats.values()):
            try:
                dlg._dock()
            except Exception:
                try:
                    dlg.close()
                except Exception:
                    pass
        self._floats.clear()
