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
        self.btn_light = QPushButton("补光(预留)")
        self.btn_focus = QPushButton("焦距(预留)")
        self.btn_light.setEnabled(False)
        self.btn_focus.setEnabled(False)
        row.addWidget(self.btn_open)
        row.addWidget(self.btn_close)
        row.addWidget(self.btn_snap)
        row.addWidget(self.btn_folder)
        row.addWidget(self.btn_light)
        row.addWidget(self.btn_focus)
        root.addLayout(row)
        self.view = QLabel("无画面")
        self.view.setAlignment(Qt.AlignCenter)
        self.view.setMinimumHeight(80)
        self.view.setStyleSheet("background:#1a1d23; border:1px solid #454c58;")
        root.addWidget(self.view, 1)
        self.btn_open.clicked.connect(self.openRequested.emit)
        self.btn_close.clicked.connect(self.closeRequested.emit)
        self.btn_snap.clicked.connect(self.snapshotRequested.emit)
        self.btn_folder.clicked.connect(self.openFolderRequested.emit)

    def show_bgr(self, frame) -> None:
        if frame is None:
            self._last_bgr = None
            self.view.setText("无画面")
            self.view.setPixmap(QPixmap())
            return
        self._last_bgr = frame
        self._paint_frame()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._last_bgr is not None:
            self._paint_frame()

    def _paint_frame(self) -> None:
        frame = self._last_bgr
        if frame is None:
            return
        rgb = frame[:, :, ::-1].copy()
        h, w, ch = rgb.shape
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888)
        self.view.setPixmap(
            QPixmap.fromImage(qimg).scaled(
                self.view.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        )
