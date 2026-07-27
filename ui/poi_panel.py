"""星标列表面板 (功能表 #9/#10/#11)。

右侧停靠的一个 QWidget:
    - QListWidget 列出底盘上的星标 (POI)
    - 按钮: 添加星标 / 前往 / 删除 / 刷新

本面板只发信号, 不直接调底盘 —— 实际的 add/goto/delete 由 MainWindow
统一经 HermesClient 执行, 保持"UI 不碰网络"的分层。

依据决策: 仅联机可用。未连接时 set_online(False) 置灰除"刷新"外的按钮。
"""

from __future__ import annotations

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class PoiPanel(QWidget):

    addModeRequested = pyqtSignal()       # 请求进入地图放置模式
    gotoRequested = pyqtSignal(str)        # 前往 poi_id
    deleteRequested = pyqtSignal(str)      # 删除 poi_id
    refreshRequested = pyqtSignal()        # 重新拉取星标
    selectionChanged = pyqtSignal(str)     # 列表选中变化 -> poi_id (空串=无)
    headingDragRequested = pyqtSignal(str)      # 进地图拖拽调朝向 poi_id
    headingValueRequested = pyqtSignal(str, float)  # 数值设朝向 (poi_id, deg)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(236)

        self.list = QListWidget()
        self.btn_add = QPushButton("✛ 添加星标")
        self.btn_add.setObjectName("primary")
        self.btn_goto = QPushButton("➤ 前往")
        self.btn_del = QPushButton("🗑 删除")
        self.btn_del.setObjectName("danger")
        self.btn_refresh = QPushButton("⟳ 刷新")
        # 朝向编辑
        self.btn_heading = QPushButton("⟲ 拖拽调朝向")
        self.spin_heading = QDoubleSpinBox()
        self.spin_heading.setRange(0, 359.9)
        self.spin_heading.setSuffix(" °")
        self.spin_heading.setDecimals(1)
        self.btn_heading_set = QPushButton("设角度")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        title = QLabel("星标点")
        title.setStyleSheet("font-weight: bold; color: #9aa3b2;")
        layout.addWidget(title)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.btn_add)
        row = QHBoxLayout()
        row.addWidget(self.btn_goto)
        row.addWidget(self.btn_del)
        layout.addLayout(row)
        # 朝向编辑区
        layout.addWidget(self.btn_heading)
        hrow = QHBoxLayout()
        hrow.addWidget(QLabel("朝向"))
        hrow.addWidget(self.spin_heading, 1)
        hrow.addWidget(self.btn_heading_set)
        layout.addLayout(hrow)
        layout.addWidget(self.btn_refresh)

        self.btn_add.clicked.connect(self.addModeRequested)
        self.btn_refresh.clicked.connect(self.refreshRequested)
        self.btn_goto.clicked.connect(self._emit_goto)
        self.btn_del.clicked.connect(self._emit_delete)
        self.btn_heading.clicked.connect(self._emit_heading_drag)
        self.btn_heading_set.clicked.connect(self._emit_heading_value)
        self.list.itemSelectionChanged.connect(self._on_selection)

        self._online = False
        self.set_online(False)

    # ---- 数据 ----

    def set_pois(self, pois) -> None:
        """刷新列表。pois: 含 .poi_id .name 的对象列表。"""
        prev = self.current_poi_id()
        self.list.blockSignals(True)
        self.list.clear()
        for poi in pois:
            it = QListWidgetItem(poi.name or poi.poi_id)
            it.setData(256, str(poi.poi_id))  # Qt.UserRole == 256
            self.list.addItem(it)
        self.list.blockSignals(False)
        # 尽量保持原选中
        if prev:
            self.select_poi(prev)

    def current_poi_id(self) -> str:
        it = self.list.currentItem()
        return str(it.data(256)) if it is not None else ""

    def select_poi(self, poi_id: str) -> None:
        """按 id 选中列表项(地图->列表联动用)。"""
        for i in range(self.list.count()):
            it = self.list.item(i)
            if str(it.data(256)) == str(poi_id):
                self.list.setCurrentItem(it)
                return

    # ---- 状态 ----

    def set_online(self, online: bool) -> None:
        """联机态: 离线时只留'刷新'可用, 其余置灰。"""
        self._online = online
        self.btn_add.setEnabled(online)
        self.btn_refresh.setEnabled(online)
        self._update_action_buttons()

    def _update_action_buttons(self) -> None:
        has_sel = self.list.currentItem() is not None
        self.btn_goto.setEnabled(self._online and has_sel)
        self.btn_del.setEnabled(self._online and has_sel)
        self.btn_heading.setEnabled(self._online and has_sel)
        self.btn_heading_set.setEnabled(self._online and has_sel)
        self.spin_heading.setEnabled(self._online and has_sel)

    def set_heading_value(self, deg: float) -> None:
        """外部(选中星标后)同步当前朝向到 spinbox 显示。"""
        self.spin_heading.blockSignals(True)
        self.spin_heading.setValue(deg % 360)
        self.spin_heading.blockSignals(False)

    # ---- 内部 ----

    def _on_selection(self) -> None:
        self._update_action_buttons()
        self.selectionChanged.emit(self.current_poi_id())

    def _emit_goto(self) -> None:
        pid = self.current_poi_id()
        if pid:
            self.gotoRequested.emit(pid)

    def _emit_heading_drag(self) -> None:
        pid = self.current_poi_id()
        if pid:
            self.headingDragRequested.emit(pid)

    def _emit_heading_value(self) -> None:
        pid = self.current_poi_id()
        if pid:
            self.headingValueRequested.emit(pid, self.spin_heading.value())

    def _emit_delete(self) -> None:
        pid = self.current_poi_id()
        if pid:
            self.deleteRequested.emit(pid)
