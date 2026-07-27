"""地图画布控件 —— 渲染 STCM 栅格图并叠加机器人/星标/充电桩。

对应功能表 #1(显示地图) #2(实时位置) #4(视角切换)。

坐标系说明:
    - 世界坐标: 米, x 向右, y 向上 (SLAM 习惯)。
    - 栅格: 行优先, row 0 在 origin_y 处, 向上递增。
    - Qt 场景坐标: y 向下。因此渲染时整体翻转 y, 并用一个
      变换把世界坐标映射进场景, 避免在多处手动翻转出错。

设计: QGraphicsView + QGraphicsScene。
    - 底图: 一个 QGraphicsPixmapItem (栅格转成的 QPixmap)。
    - 机器人: 一个箭头 item, 每帧更新位置/朝向。
    - 星标/充电桩: 标记 item。
鼠标滚轮缩放, 拖拽平移 (功能表 #4 的全屏/自由视角基础)。
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
from PyQt5.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PyQt5.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsPolygonItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
)

from devices.chassis.stcm import GridMap


def grid_to_qimage(grid: GridMap) -> QImage:
    """占用栅格 -> QImage(灰度)。

    占用值约定 (实机逆向): 0 附近=空闲(白), 127=未知(灰), 128+=障碍(黑)。
    """
    arr = np.frombuffer(grid.cells, dtype=np.uint8).reshape(
        grid.height, grid.width
    )
    img = np.empty_like(arr)
    img[arr < 127] = 255   # 空闲 -> 白
    img[arr == 127] = 190  # 未知 -> 浅灰
    img[arr > 127] = 40    # 障碍 -> 近黑
    # STCM row 0 在底部(y 小), 图像 row 0 在顶部 -> 上下翻转
    img = np.flipud(img).copy()
    h, w = img.shape
    qimg = QImage(img.data, w, h, w, QImage.Format_Grayscale8)
    # QImage 不持有 numpy 内存, 复制一份独立持有
    return qimg.copy()


class MapCanvas(QGraphicsView):
    """可缩放/平移的地图视图。

    用法:
        canvas = MapCanvas()
        canvas.load_grid(grid)          # 载入 STCM 栅格
        canvas.update_robot(x, y, yaw)  # 刷新机器人位姿(世界坐标,米/弧度)
    """

    # 放置模式下左键点击地图, 发出世界坐标 (x, y) 米
    placeRequested = pyqtSignal(float, float)
    # 点击已有星标, 发出其 poi_id
    poiClicked = pyqtSignal(str)
    # 画墙模式下拖拽完成, 发出世界坐标 (x1, y1, x2, y2) 米
    wallDrawn = pyqtSignal(float, float, float, float)
    # 重定位模式下拖拽完成, 发出世界坐标位置+朝向 (x, y, yaw弧度)
    relocRequested = pyqtSignal(float, float, float)
    # 星标朝向拖拽完成, 发出 (poi_id, yaw弧度)
    poiHeadingChanged = pyqtSignal(str, float)
    # 画轨道折线完成, 发出顶点世界坐标列表 [(x,y),...]
    trackDrawn = pyqtSignal(list)
    # 导航模式下点击地图, 发出目标世界坐标 (x, y)
    navRequested = pyqtSignal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setBackgroundBrush(QColor(0x23, 0x27, 0x2e))  # 主题深色 #23272e

        self._grid: Optional[GridMap] = None
        self._map_item: Optional[QGraphicsPixmapItem] = None
        self._robot_item: Optional[QGraphicsPolygonItem] = None
        self._dock_item: Optional[QGraphicsEllipseItem] = None
        self._poi_items: list = []
        self._poi_by_id: dict = {}      # poi_id -> ellipse item
        self._wall_items: list = []     # 已渲染的虚拟墙线段
        self._laser_item: Optional[QGraphicsPathItem] = None  # 激光点云(单 item)
        self._follow_robot = False
        self._place_mode = False
        self._highlighted: Optional[str] = None
        # 画墙模式
        self._wall_mode = False
        self._wall_start: Optional[QPointF] = None  # 场景坐标
        self._wall_preview: Optional[QGraphicsLineItem] = None
        # 重定位模式(点位置+拖朝向)
        self._reloc_mode = False
        self._reloc_start: Optional[QPointF] = None       # 场景坐标
        self._reloc_preview: Optional[QGraphicsLineItem] = None
        # 星标朝向编辑模式
        self._heading_mode = False
        self._heading_poi: Optional[str] = None
        self._heading_start: Optional[QPointF] = None     # 星标场景坐标
        self._heading_preview: Optional[QGraphicsLineItem] = None
        # 轨道
        self._poi_arrow_items: list = []   # 星标朝向箭头
        self._poi_label_items: list = []   # 星标名称文字标签
        self._track_items: list = []       # 已渲染轨道线段
        # 画轨道(多点折线)模式
        self._track_mode = False
        self._track_pts: list = []         # 已落顶点(场景坐标)
        self._track_preview_items: list = []  # 折线预览(已固定段 + 跟随段)
        # 导航模式 + 规划路线
        self._nav_mode = False
        self._path_item: Optional[QGraphicsPathItem] = None

    # ---- 坐标换算: 世界坐标(米) <-> 场景坐标(像素, 1格=1像素) ----

    def world_to_scene(self, x: float, y: float) -> QPointF:
        """世界坐标 -> 场景坐标。

        场景以栅格像素为单位, 底图左上角为 (0,0)。
        世界 y 向上, 场景 y 向下, 故场景行 = (height-1) - 栅格行。
        """
        g = self._grid
        col = (x - g.origin_x) / g.resolution
        row = (y - g.origin_y) / g.resolution
        scene_y = (g.height - 1) - row
        return QPointF(col, scene_y)

    def scene_to_world(self, pt: QPointF) -> tuple:
        """场景坐标 -> 世界坐标(米)。world_to_scene 的逆运算。"""
        g = self._grid
        col = pt.x()
        row = (g.height - 1) - pt.y()
        x = g.origin_x + col * g.resolution
        y = g.origin_y + row * g.resolution
        return x, y

    # ---- 载入地图 ----

    def load_grid(self, grid: GridMap) -> None:
        self._grid = grid
        self._scene.clear()
        self._map_item = None
        self._robot_item = None
        self._dock_item = None
        self._poi_items = []
        self._poi_by_id = {}
        self._poi_arrow_items = []
        self._poi_label_items = []
        self._wall_items = []
        self._track_items = []
        self._laser_item = None
        self._path_item = None
        self._highlighted = None

        qimg = grid_to_qimage(grid)
        self._map_item = QGraphicsPixmapItem(QPixmap.fromImage(qimg))
        self._map_item.setZValue(0)
        self._scene.addItem(self._map_item)
        self._scene.setSceneRect(QRectF(0, 0, grid.width, grid.height))
        self.fit_view()

    def fit_view(self) -> None:
        """缩放使整图可见(功能表 #4 全屏视角)。"""
        if self._map_item is not None:
            self.fitInView(self._map_item, Qt.KeepAspectRatio)

    # ---- 机器人位姿 (功能表 #2 实时位置) ----

    def update_robot(self, x: float, y: float, yaw: float) -> None:
        """更新机器人位置与朝向(世界坐标, 米/弧度)。"""
        if self._grid is None:
            return
        if self._robot_item is None:
            # 一个指向 +x 的三角箭头, 以 0.25m 为尺度(用格数表示)
            s = max(3.0, 0.25 / self._grid.resolution)
            poly = QPolygonF([
                QPointF(s, 0), QPointF(-s * 0.6, s * 0.6),
                QPointF(-s * 0.6, -s * 0.6),
            ])
            self._robot_item = QGraphicsPolygonItem(poly)
            self._robot_item.setBrush(QBrush(QColor(220, 40, 40)))
            self._robot_item.setPen(QPen(QColor(120, 0, 0), 0))
            self._robot_item.setZValue(10)
            self._scene.addItem(self._robot_item)

        pt = self.world_to_scene(x, y)
        self._robot_item.setPos(pt)
        # 世界 yaw 逆时针为正(y 上); 场景 y 向下, 故旋转取负, 转角度
        self._robot_item.setRotation(-math.degrees(yaw))

        if self._follow_robot:
            self.centerOn(pt)

    def set_follow_robot(self, follow: bool) -> None:
        """机器人视角: 视图始终跟随机器人(功能表 #4)。"""
        self._follow_robot = follow

    # ---- 充电桩 (功能表 #3) ----

    def set_dock(self, x: float, y: float) -> None:
        if self._grid is None:
            return
        if self._dock_item is None:
            r = max(3.0, 0.2 / self._grid.resolution)
            self._dock_item = QGraphicsEllipseItem(-r, -r, 2 * r, 2 * r)
            self._dock_item.setBrush(QBrush(QColor(40, 160, 60)))
            self._dock_item.setPen(QPen(QColor(0, 80, 0), 0))
            self._dock_item.setZValue(5)
            self._scene.addItem(self._dock_item)
        self._dock_item.setPos(self.world_to_scene(x, y))

    # ---- 星标 (功能表 #9) ----

    def set_pois(self, pois) -> None:
        """重画所有星标点 + 朝向箭头。pois: 含 .x .y .yaw .poi_id .name。"""
        for it in self._poi_items:
            self._scene.removeItem(it)
        for it in self._poi_arrow_items:
            self._scene.removeItem(it)
        for it in self._poi_label_items:
            self._scene.removeItem(it)
        self._poi_items = []
        self._poi_arrow_items = []
        self._poi_label_items = []
        self._poi_by_id = {}
        if self._grid is None:
            return
        r = max(2.5, 0.15 / self._grid.resolution)
        for poi in pois:
            it = QGraphicsEllipseItem(-r, -r, 2 * r, 2 * r)
            it.setBrush(QBrush(QColor(50, 120, 230)))
            it.setPen(QPen(QColor(20, 50, 120), 0))
            it.setZValue(6)
            it.setPos(self.world_to_scene(poi.x, poi.y))
            it.setToolTip(getattr(poi, "name", ""))
            pid = str(getattr(poi, "poi_id", ""))
            it.setData(0, pid)
            self._scene.addItem(it)
            self._poi_items.append(it)
            if pid:
                self._poi_by_id[pid] = it
            # 朝向箭头(小三角, 指向 +x 再按 yaw 旋转)
            arrow = self._make_heading_arrow(r)
            arrow.setPos(self.world_to_scene(poi.x, poi.y))
            arrow.setRotation(-math.degrees(getattr(poi, "yaw", 0.0) or 0.0))
            arrow.setData(0, pid)
            self._scene.addItem(arrow)
            self._poi_arrow_items.append(arrow)
            # 名称标签(常驻显示, 不随缩放变大/变小, 便于查看)
            name = getattr(poi, "name", "") or ""
            if name:
                self._add_poi_label(poi, name, r)
        # 重画后恢复高亮
        if self._highlighted:
            self.highlight_poi(self._highlighted)

    def _add_poi_label(self, poi, name: str, r: float) -> None:
        """在星标旁绘制名称文字。用 ItemIgnoresTransformations 保持恒定字号,
        缩放地图时文字大小不变, 始终清晰可读。"""
        label = QGraphicsSimpleTextItem(name)
        font = QFont()
        font.setPointSize(13)
        font.setBold(True)
        label.setFont(font)
        label.setBrush(QBrush(QColor(0, 0, 0)))          # 黑色文字
        label.setPen(QPen(Qt.NoPen))                     # 无描边
        label.setZValue(7)
        label.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        # 锚在星标右上方, 略偏移避免压住标记点
        label.setPos(self.world_to_scene(poi.x, poi.y) + QPointF(r * 0.9, -r * 1.8))
        label.setData(0, str(getattr(poi, "poi_id", "")))
        self._scene.addItem(label)
        self._poi_label_items.append(label)

    def _make_heading_arrow(self, r: float) -> QGraphicsPolygonItem:
        """构造星标朝向小箭头(指向 +x, 长度约 2.2r), 蓝色。"""
        L = r * 2.6
        w = r * 0.9
        poly = QPolygonF([QPointF(L, 0), QPointF(L - w, w * 0.6),
                          QPointF(L - w, -w * 0.6)])
        arrow = QGraphicsPolygonItem(poly)
        arrow.setBrush(QBrush(QColor(90, 160, 255)))
        arrow.setPen(QPen(QColor(20, 50, 120), 0))
        arrow.setZValue(6)
        return arrow

    def highlight_poi(self, poi_id: Optional[str]) -> None:
        """高亮选中的星标(描红圈), 传 None 取消。"""
        # 还原上一个
        prev = self._poi_by_id.get(self._highlighted) if self._highlighted else None
        if prev is not None:
            prev.setPen(QPen(QColor(20, 50, 120), 0))
            prev.setBrush(QBrush(QColor(50, 120, 230)))
        self._highlighted = poi_id
        cur = self._poi_by_id.get(poi_id) if poi_id else None
        if cur is not None:
            cur.setPen(QPen(QColor(230, 40, 40), 0))
            cur.setBrush(QBrush(QColor(255, 170, 60)))

    # ---- 激光点云 (雷达实时渲染) ----

    def set_laser_points(self, world_points) -> None:
        """更新激光点云。world_points: [(x, y), ...] 世界坐标(米)。

        高频刷新: 用单个 QGraphicsPathItem 承载所有点(每点一个小方块),
        每帧只替换这一个 item 的 path, 避免成百上千 item 的增删开销。
        """
        if self._grid is None:
            return
        if self._laser_item is None:
            self._laser_item = QGraphicsPathItem()
            # 点用红色, 无描边, 实心
            self._laser_item.setPen(QPen(Qt.NoPen))
            self._laser_item.setBrush(QBrush(QColor(230, 40, 40)))
            self._laser_item.setZValue(8)   # 在地图/墙之上, 机器人之下
            self._scene.addItem(self._laser_item)
        path = QPainterPath()
        r = max(0.6, 0.04 / self._grid.resolution)  # 点半径(场景像素)
        d = r * 2
        for x, y in world_points:
            pt = self.world_to_scene(x, y)
            path.addRect(pt.x() - r, pt.y() - r, d, d)
        self._laser_item.setPath(path)

    def clear_laser(self) -> None:
        if self._laser_item is not None:
            self._laser_item.setPath(QPainterPath())

    # ---- 放置模式 (功能表 #10 添加星标) ----

    def set_place_mode(self, on: bool) -> None:
        """开启放置模式: 下一次左键点击地图发 placeRequested。"""
        self._place_mode = on
        if on:
            self.setDragMode(QGraphicsView.NoDrag)
            self.viewport().setCursor(Qt.CrossCursor)
        else:
            self.setDragMode(QGraphicsView.ScrollHandDrag)
            self.viewport().unsetCursor()

    # ---- 虚拟墙 (功能表 #7) ----

    def set_walls(self, walls) -> None:
        """渲染虚拟墙。walls: [(id, x1, y1, x2, y2), ...] 世界坐标(米)。"""
        for it in self._wall_items:
            self._scene.removeItem(it)
        self._wall_items = []
        if self._grid is None:
            return
        pen = QPen(QColor(230, 80, 80), 0)
        pen.setCosmetic(True)  # 缩放时线宽不变
        for w in walls:
            _id, x1, y1, x2, y2 = w
            p1 = self.world_to_scene(x1, y1)
            p2 = self.world_to_scene(x2, y2)
            it = QGraphicsLineItem(p1.x(), p1.y(), p2.x(), p2.y())
            it.setPen(pen)
            it.setZValue(4)
            self._scene.addItem(it)
            self._wall_items.append(it)

    def set_tracks(self, tracks) -> None:
        """渲染虚拟轨道。tracks: [(id, x1, y1, x2, y2), ...] 世界坐标(米)。

        绿色虚线, 与墙(红实线)区分。
        """
        for it in self._track_items:
            self._scene.removeItem(it)
        self._track_items = []
        if self._grid is None:
            return
        pen = QPen(QColor(60, 200, 110), 0)
        pen.setCosmetic(True)
        pen.setWidth(3)            # 加粗, 实机反馈太细
        pen.setStyle(Qt.DashLine)
        for tk in tracks:
            _id, x1, y1, x2, y2 = tk
            p1 = self.world_to_scene(x1, y1)
            p2 = self.world_to_scene(x2, y2)
            it = QGraphicsLineItem(p1.x(), p1.y(), p2.x(), p2.y())
            it.setPen(pen)
            it.setZValue(4)
            self._scene.addItem(it)
            self._track_items.append(it)

    def set_wall_mode(self, on: bool) -> None:
        """开启画墙模式: 在地图上按下拖拽到松开, 画一条虚拟墙。"""
        self._wall_mode = on
        self._wall_start = None
        if self._wall_preview is not None:
            self._scene.removeItem(self._wall_preview)
            self._wall_preview = None
        if on:
            self.setDragMode(QGraphicsView.NoDrag)
            self.viewport().setCursor(Qt.CrossCursor)
        else:
            self.setDragMode(QGraphicsView.ScrollHandDrag)
            self.viewport().unsetCursor()

    def set_reloc_mode(self, on: bool) -> None:
        """开启重定位模式: 按下=机器人真实位置, 拖拽=朝向, 松开提交。"""
        self._reloc_mode = on
        self._reloc_start = None
        if self._reloc_preview is not None:
            self._scene.removeItem(self._reloc_preview)
            self._reloc_preview = None
        if on:
            self.setDragMode(QGraphicsView.NoDrag)
            self.viewport().setCursor(Qt.CrossCursor)
        else:
            self.setDragMode(QGraphicsView.ScrollHandDrag)
            self.viewport().unsetCursor()

    def set_poi_heading_mode(self, poi_id: str, x: float, y: float) -> None:
        """开启某星标的朝向编辑: 从星标位置拖拽指向朝向, 松开发 poiHeadingChanged。"""
        self._heading_mode = True
        self._heading_poi = poi_id
        self._heading_start = self.world_to_scene(x, y)
        if self._heading_preview is not None:
            self._scene.removeItem(self._heading_preview)
            self._heading_preview = None
        self.setDragMode(QGraphicsView.NoDrag)
        self.viewport().setCursor(Qt.CrossCursor)

    def _end_heading_mode(self) -> None:
        self._heading_mode = False
        self._heading_poi = None
        self._heading_start = None
        if self._heading_preview is not None:
            self._scene.removeItem(self._heading_preview)
            self._heading_preview = None
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.viewport().unsetCursor()

    def set_track_mode(self, on: bool) -> None:
        """开启画轨道模式: 左键依次落点连成折线, 右键/双击结束。"""
        self._track_mode = on
        self._track_pts = []
        for it in self._track_preview_items:
            self._scene.removeItem(it)
        self._track_preview_items = []
        if on:
            self.setDragMode(QGraphicsView.NoDrag)
            self.viewport().setCursor(Qt.CrossCursor)
        else:
            self.setDragMode(QGraphicsView.ScrollHandDrag)
            self.viewport().unsetCursor()

    def _redraw_track_preview(self, cur: QPointF = None) -> None:
        """重画折线预览(已落点连线 + 到当前鼠标的跟随段)。"""
        for it in self._track_preview_items:
            self._scene.removeItem(it)
        self._track_preview_items = []
        pen = QPen(QColor(60, 200, 110), 0)
        pen.setCosmetic(True)
        pen.setWidth(3)            # 加粗, 实机反馈太细
        pen.setStyle(Qt.DashLine)
        pts = list(self._track_pts)
        if cur is not None:
            pts = pts + [cur]
        for i in range(len(pts) - 1):
            ln = QGraphicsLineItem(pts[i].x(), pts[i].y(),
                                   pts[i + 1].x(), pts[i + 1].y())
            ln.setPen(pen)
            ln.setZValue(20)
            self._scene.addItem(ln)
            self._track_preview_items.append(ln)

    def _finish_track(self) -> None:
        """结束折线绘制, 发顶点世界坐标列表。"""
        pts = list(self._track_pts)
        self.set_track_mode(False)
        if len(pts) >= 2:
            world = [self.scene_to_world(p) for p in pts]
            QTimer.singleShot(0, lambda: self.trackDrawn.emit(world))

    # ---- 导航 (点击地图导航 + 规划路线) ----

    def set_nav_mode(self, on: bool) -> None:
        """开启导航模式: 点地图任意处发 navRequested(点一次走一次)。"""
        self._nav_mode = on
        if on:
            self.setDragMode(QGraphicsView.NoDrag)
            self.viewport().setCursor(Qt.CrossCursor)
        else:
            self.setDragMode(QGraphicsView.ScrollHandDrag)
            self.viewport().unsetCursor()

    def set_path(self, world_points) -> None:
        """渲染规划/剩余路线。world_points: [(x,y),...] 世界坐标。空则清空。"""
        if self._grid is None:
            return
        if self._path_item is None:
            pen = QPen(QColor(80, 200, 255), 0)   # 青蓝色实线
            pen.setCosmetic(True)
            pen.setWidth(3)
            self._path_item = QGraphicsPathItem()
            self._path_item.setPen(pen)
            self._path_item.setZValue(9)   # 在轨道/墙之上, 机器人之下
            self._scene.addItem(self._path_item)
        path = QPainterPath()
        pts = list(world_points or [])
        if pts:
            p0 = self.world_to_scene(pts[0][0], pts[0][1])
            path.moveTo(p0)
            for x, y in pts[1:]:
                p = self.world_to_scene(x, y)
                path.lineTo(p)
        self._path_item.setPath(path)

    def clear_path(self) -> None:
        if self._path_item is not None:
            self._path_item.setPath(QPainterPath())

    # ---- 交互 ----

    def mousePressEvent(self, event):
        # 画轨道模式: 左键落点, 右键结束(右键也要响应, 故先于 LeftButton 判断)
        if self._track_mode and self._grid is not None:
            if event.button() == Qt.LeftButton:
                self._track_pts.append(self.mapToScene(event.pos()))
                self._redraw_track_preview()
                return
            if event.button() == Qt.RightButton:
                self._finish_track()
                return
        if self._grid is not None and event.button() == Qt.LeftButton:
            scene_pt = self.mapToScene(event.pos())
            # 朝向编辑模式: 按下开始拖拽(起点固定为星标位置)
            if self._heading_mode and self._heading_start is not None:
                pen = QPen(QColor(90, 160, 255), 0)
                pen.setCosmetic(True)
                pen.setWidth(2)
                s = self._heading_start
                self._heading_preview = QGraphicsLineItem(
                    s.x(), s.y(), scene_pt.x(), scene_pt.y()
                )
                self._heading_preview.setPen(pen)
                self._heading_preview.setZValue(20)
                self._scene.addItem(self._heading_preview)
                return
            # 放置模式: 点哪建哪, 然后退出放置模式
            if self._place_mode:
                x, y = self.scene_to_world(scene_pt)
                self.set_place_mode(False)
                QTimer.singleShot(0, lambda: self.placeRequested.emit(x, y))
                return
            # 导航模式: 点哪去哪, 然后退出导航模式
            if self._nav_mode:
                x, y = self.scene_to_world(scene_pt)
                self.set_nav_mode(False)
                QTimer.singleShot(0, lambda: self.navRequested.emit(x, y))
                return
            # 画墙模式: 记起点, 开始拖拽预览
            if self._wall_mode:
                self._wall_start = scene_pt
                pen = QPen(QColor(230, 80, 80), 0)
                pen.setCosmetic(True)
                self._wall_preview = QGraphicsLineItem(
                    scene_pt.x(), scene_pt.y(), scene_pt.x(), scene_pt.y()
                )
                self._wall_preview.setPen(pen)
                self._wall_preview.setZValue(20)
                self._scene.addItem(self._wall_preview)
                return
            # 重定位模式: 记起点(机器人位置), 拖拽指朝向
            if self._reloc_mode:
                self._reloc_start = scene_pt
                pen = QPen(QColor(60, 220, 220), 0)  # 青色, 与红箭头区分
                pen.setCosmetic(True)
                pen.setWidth(2)
                self._reloc_preview = QGraphicsLineItem(
                    scene_pt.x(), scene_pt.y(), scene_pt.x(), scene_pt.y()
                )
                self._reloc_preview.setPen(pen)
                self._reloc_preview.setZValue(20)
                self._scene.addItem(self._reloc_preview)
                return
            # 否则: 命中已有星标则发选中信号(不拦截平移)
            item = self._scene.itemAt(scene_pt, self.transform())
            pid = item.data(0) if item is not None else None
            if pid:
                self.poiClicked.emit(str(pid))
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # 画轨道: 跟随段预览(从最后落点到当前鼠标)
        if self._track_mode and self._track_pts:
            self._redraw_track_preview(self.mapToScene(event.pos()))
            return
        # 朝向拖拽中: 更新预览线
        if self._heading_mode and self._heading_start is not None and \
                self._heading_preview is not None:
            cur = self.mapToScene(event.pos())
            self._heading_preview.setLine(
                self._heading_start.x(), self._heading_start.y(),
                cur.x(), cur.y()
            )
            return
        # 画墙拖拽中: 更新预览线终点
        if self._wall_mode and self._wall_start is not None and \
                self._wall_preview is not None:
            cur = self.mapToScene(event.pos())
            self._wall_preview.setLine(
                self._wall_start.x(), self._wall_start.y(), cur.x(), cur.y()
            )
            return
        # 重定位拖拽中: 更新朝向预览线
        if self._reloc_mode and self._reloc_start is not None and \
                self._reloc_preview is not None:
            cur = self.mapToScene(event.pos())
            self._reloc_preview.setLine(
                self._reloc_start.x(), self._reloc_start.y(), cur.x(), cur.y()
            )
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        # 朝向编辑完成: 起点=星标, 拖拽方向=朝向(世界系 atan2)
        if self._heading_mode and self._heading_start is not None and \
                event.button() == Qt.LeftButton:
            sx, sy = self.scene_to_world(self._heading_start)
            ex, ey = self.scene_to_world(self.mapToScene(event.pos()))
            pid = self._heading_poi
            self._end_heading_mode()
            yaw = math.atan2(ey - sy, ex - sx)
            if pid:
                # 延迟到事件循环外再发: 信号会触发 set_pois 删除/重建图元,
                # 在 release 事件内同步重建正在交互的 item 会导致崩溃。
                QTimer.singleShot(
                    0, lambda: self.poiHeadingChanged.emit(str(pid), yaw))
            return
        # 画墙完成: 发世界坐标的起止点
        if self._wall_mode and self._wall_start is not None and \
                event.button() == Qt.LeftButton:
            end = self.mapToScene(event.pos())
            x1, y1 = self.scene_to_world(self._wall_start)
            x2, y2 = self.scene_to_world(end)
            if self._wall_preview is not None:
                self._scene.removeItem(self._wall_preview)
                self._wall_preview = None
            self._wall_start = None
            self.set_wall_mode(False)
            # 太短的拖拽忽略(误点)
            if abs(x2 - x1) > 0.05 or abs(y2 - y1) > 0.05:
                QTimer.singleShot(
                    0, lambda: self.wallDrawn.emit(x1, y1, x2, y2))
            return
        # 重定位完成: 起点=位置, 拖拽方向=朝向(世界系 atan2)
        if self._reloc_mode and self._reloc_start is not None and \
                event.button() == Qt.LeftButton:
            start = self._reloc_start
            end = self.mapToScene(event.pos())
            x, y = self.scene_to_world(start)
            ex, ey = self.scene_to_world(end)
            if self._reloc_preview is not None:
                self._scene.removeItem(self._reloc_preview)
                self._reloc_preview = None
            self._reloc_start = None
            self.set_reloc_mode(False)
            # 世界系朝向(用世界坐标算, 避免场景 y 翻转出错)
            yaw = math.atan2(ey - y, ex - x)
            QTimer.singleShot(0, lambda: self.relocRequested.emit(x, y, yaw))
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def mouseDoubleClickEvent(self, event):
        # 画轨道双击结束(双击会先来一次单击落点, 这里收尾)
        if self._track_mode:
            self._finish_track()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        # Esc 取消当前绘制/编辑模式
        if event.key() == Qt.Key_Escape:
            if self._track_mode:
                self.set_track_mode(False)
                return
            if self._heading_mode:
                self._end_heading_mode()
                return
            if self._wall_mode:
                self.set_wall_mode(False)
                return
            if self._reloc_mode:
                self.set_reloc_mode(False)
                return
            if self._nav_mode:
                self.set_nav_mode(False)
                return
        super().keyPressEvent(event)
