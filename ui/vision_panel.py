"""视觉面板。"""

from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QImage, QPixmap
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class VisionPanel(QWidget):
    openRequested = pyqtSignal()
    closeRequested = pyqtSignal()
    snapshotRequested = pyqtSignal()
    openFolderRequested = pyqtSignal()
    zoomStartRequested = pyqtSignal(int)   # +1 放大 / -1 缩小
    ptzStopRequested = pyqtSignal()
    autoZoomToggled = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(180)
        self._last_bgr = None
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        row = QHBoxLayout()
        self.btn_open = QPushButton("打开摄像头")
        self.btn_open.setObjectName("primary")
        self.btn_close = QPushButton("关闭")
        self.btn_snap = QPushButton("抓拍")
        self.btn_folder = QPushButton("抓拍目录")
        self.btn_folder.setToolTip("打开抓拍图片保存文件夹")
        row.addWidget(self.btn_open)
        row.addWidget(self.btn_close)
        row.addWidget(self.btn_snap)
        row.addWidget(self.btn_folder)
        root.addLayout(row)

        zoom_row = QHBoxLayout()
        self.btn_zoom_out = QPushButton("变焦-")
        self.btn_zoom_out.setToolTip("按住缩小（电动变倍）；松开停止")
        self.btn_zoom_in = QPushButton("变焦+")
        self.btn_zoom_in.setToolTip("按住放大（电动变倍）；松开停止")
        self._tip_zoom = self.btn_zoom_in.toolTip()
        self.btn_auto_zoom = QPushButton("自动变焦")
        self.btn_auto_zoom.setObjectName("toggle")
        self.btn_auto_zoom.setCheckable(True)
        self.btn_auto_zoom.setChecked(True)
        self.btn_auto_zoom.setToolTip("测距>1000mm或过远→最长焦，≤1000mm→最短焦；再点取消")
        self.btn_light = QPushButton("补光(预留)")
        self.btn_light.setEnabled(False)
        zoom_row.addWidget(self.btn_auto_zoom)
        zoom_row.addWidget(self.btn_zoom_out)
        zoom_row.addWidget(self.btn_zoom_in)
        zoom_row.addWidget(self.btn_light)
        root.addLayout(zoom_row)

        self.view = QLabel("无画面")
        self.view.setAlignment(Qt.AlignCenter)
        self.view.setMinimumHeight(80)
        self.view.setStyleSheet("background:#1a1d23; border:1px solid #454c58;")
        root.addWidget(self.view, 1)
        self.btn_open.clicked.connect(self.openRequested.emit)
        self.btn_close.clicked.connect(self.closeRequested.emit)
        self.btn_snap.clicked.connect(self.snapshotRequested.emit)
        self.btn_folder.clicked.connect(self.openFolderRequested.emit)
        self._bind_hold(self.btn_zoom_out, lambda: self.zoomStartRequested.emit(-1))
        self._bind_hold(self.btn_zoom_in, lambda: self.zoomStartRequested.emit(1))
        self.btn_auto_zoom.toggled.connect(self.autoZoomToggled.emit)

    def is_auto_zoom(self) -> bool:
        return bool(self.btn_auto_zoom.isChecked())

    def set_auto_zoom(self, on: bool) -> None:
        self.btn_auto_zoom.blockSignals(True)
        self.btn_auto_zoom.setChecked(bool(on))
        self.btn_auto_zoom.blockSignals(False)

    def _bind_hold(self, btn: QPushButton, on_press) -> None:
        btn.pressed.connect(on_press)
        btn.released.connect(self.ptzStopRequested.emit)

    def set_ptz_enabled(self, on: bool, *, zoom: bool | None = None, detail: str = "") -> None:
        z = bool(on) if zoom is None else bool(zoom)
        self.btn_zoom_out.setEnabled(z)
        self.btn_zoom_in.setEnabled(z)
        self.btn_auto_zoom.setEnabled(z)
        why = (detail or "").strip()
        tip = self._tip_zoom if z else (why or "本机不支持电动变焦")
        self.btn_zoom_in.setToolTip(tip)
        self.btn_zoom_out.setToolTip(tip)

    def show_bgr(self, frame) -> None:
        if frame is None:
            self._last_bgr = None
            self.view.setText("无画面")
            self.view.setPixmap(QPixmap())
            return
        self._last_bgr = frame
        self._paint_frame()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._last_bgr is not None:
            self._paint_frame()

    def _paint_frame(self) -> None:
        frame = self._last_bgr
        if frame is None:
            return
        rgb = frame[:, :, ::-1]
        if not rgb.flags["C_CONTIGUOUS"]:
            rgb = rgb.copy()
        h, w, ch = rgb.shape
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888)
        self.view.setPixmap(
            QPixmap.fromImage(qimg.copy()).scaled(
                self.view.size(), Qt.KeepAspectRatio, Qt.FastTransformation
            )
        )
