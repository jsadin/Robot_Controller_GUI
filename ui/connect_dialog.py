"""启动连接对话框 —— 程序入口, 先填底盘 IP/端口并验证连通, 再进主界面。

只有 ping 成功才放行(accept), 把验证过的 host/port 交给主窗口。
这样用户不会在没连上时面对一个空主界面。
"""

from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIntValidator
from PyQt5.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from devices.chassis import HermesClient, HermesError


class ConnectDialog(QDialog):
    """连接底盘对话框。accept 后从 host()/port() 取已验证的地址。"""

    def __init__(self, host: str = "192.168.11.1", port: int = 1448,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("连接 Hermes 底盘")
        self.setMinimumWidth(320)
        self._host = host
        self._port = port

        self.ip_edit = QLineEdit(host)
        self.port_edit = QLineEdit(str(port))
        self.port_edit.setValidator(QIntValidator(1, 65535, self))

        form = QFormLayout()
        form.addRow("底盘 IP:", self.ip_edit)
        form.addRow("端口:", self.port_edit)

        self.hint = QLabel("电脑需先连到底盘热点 SLAMWARE-XXXXXX")
        self.hint.setStyleSheet("color: gray;")
        self.hint.setWordWrap(True)

        self.btn_connect = QPushButton("连接")
        self.btn_connect.setObjectName("primary")
        self.btn_cancel = QPushButton("取消")
        self.btn_connect.setDefault(True)
        self.btn_connect.clicked.connect(self._try_connect)
        self.btn_cancel.clicked.connect(self.reject)

        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(self.btn_connect)
        btns.addWidget(self.btn_cancel)

        title = QLabel("连接 Hermes 底盘")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        subtitle = QLabel("填写底盘 IP 与端口, 连通后进入主界面")
        subtitle.setStyleSheet("color: #9aa3b2;")

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)
        root.addWidget(title)
        root.addWidget(subtitle)
        root.addLayout(form)
        root.addWidget(self.hint)
        root.addLayout(btns)

    def host(self) -> str:
        return self._host

    def port(self) -> int:
        return self._port

    def _try_connect(self) -> None:
        host = self.ip_edit.text().strip()
        try:
            port = int(self.port_edit.text())
        except ValueError:
            self.hint.setText("端口号无效")
            self.hint.setStyleSheet("color: #c00;")
            return
        if not host:
            self.hint.setText("请填写底盘 IP")
            self.hint.setStyleSheet("color: #c00;")
            return

        self.btn_connect.setEnabled(False)
        self.hint.setText("正在连接…")
        self.hint.setStyleSheet("color: gray;")
        # 立即刷新一下界面文字
        self.repaint()
        try:
            HermesClient(host, port, timeout=4.0).ping()
        except HermesError as e:
            self.hint.setText(f"连接失败: {e}")
            self.hint.setStyleSheet("color: #c00;")
            self.btn_connect.setEnabled(True)
            return
        # 成功
        self._host = host
        self._port = port
        self.accept()
