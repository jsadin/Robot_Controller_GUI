"""集中式深色主题 (Qt StyleSheet)。

调色板:
    背景  #23272e   面板 #2b3038   控件 #363c46   控件悬停 #404754
    主蓝  #3d8bfd   主蓝按下 #2f6fd0
    文字  #e6e9ef   次要文字 #9aa3b2   边框 #454c58
    危险  #e05555   成功 #3fb950
用法: main() 里 apply_theme(app)。想换主题只改这一处。
"""

# 画布背景色, 与主题统一(map_canvas 引用)
CANVAS_BG = "#23272e"

BG = "#23272e"
PANEL = "#2b3038"
CTRL = "#363c46"
CTRL_HOVER = "#404754"
ACCENT = "#3d8bfd"
ACCENT_DOWN = "#2f6fd0"
TEXT = "#e6e9ef"
TEXT_DIM = "#9aa3b2"
BORDER = "#454c58"
DANGER = "#e05555"
SUCCESS = "#3fb950"

DARK_QSS = f"""
QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 13px;
}}
QMainWindow, QDialog {{ background-color: {BG}; }}

/* 按钮 */
QPushButton {{
    background-color: {CTRL};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 9px;
    color: {TEXT};
}}
QPushButton:hover {{ background-color: {CTRL_HOVER}; }}
QPushButton:pressed {{ background-color: {ACCENT_DOWN}; }}
QPushButton:disabled {{ color: #5b6370; border-color: #353b44; }}

/* 主操作按钮 (objectName=primary) */
QPushButton#primary {{
    background-color: {ACCENT};
    border: 1px solid {ACCENT};
    color: #ffffff;
    font-weight: bold;
}}
QPushButton#primary:hover {{ background-color: #4d97ff; }}
QPushButton#primary:pressed {{ background-color: {ACCENT_DOWN}; }}
QPushButton#primary:disabled {{ background-color: #34506f; border-color: #34506f; color: #8fa3bd; }}

/* 危险按钮 (objectName=danger) */
QPushButton#danger {{ border-color: {DANGER}; color: #ff9b9b; }}
QPushButton#danger:hover {{ background-color: {DANGER}; color: #ffffff; }}

/* 输入框 / 下拉 */
QLineEdit, QComboBox, QTimeEdit, QSpinBox {{
    background-color: {CTRL};
    border: 1px solid {BORDER};
    border-radius: 5px;
    padding: 4px 6px;
    color: {TEXT};
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QComboBox:focus, QTimeEdit:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background-color: {PANEL};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
    color: {TEXT};
}}

/* 列表 */
QListWidget {{
    background-color: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 2px;
    outline: none;
}}
QListWidget::item {{ padding: 5px 6px; border-radius: 4px; }}
QListWidget::item:selected {{ background-color: {ACCENT}; color: #ffffff; }}
QListWidget::item:hover {{ background-color: {CTRL_HOVER}; }}

/* 分组框 */
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 8px;
    margin-top: 18px;
    padding-top: 8px;
    font-weight: bold;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {TEXT_DIM};
}}

/* 标签页 */
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 6px; top: -1px; }}
QTabBar::tab {{
    background: {CTRL};
    color: {TEXT_DIM};
    padding: 7px 16px;
    border: 1px solid {BORDER};
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    margin-right: 2px;
}}
QTabBar::tab:selected {{ background: {ACCENT}; color: #ffffff; }}
QTabBar::tab:hover:!selected {{ background: {CTRL_HOVER}; color: {TEXT}; }}

/* 复选框 */
QCheckBox {{ spacing: 6px; }}
QCheckBox::indicator {{
    width: 15px; height: 15px; border-radius: 3px;
    border: 1px solid {BORDER}; background: {CTRL};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

/* 滑块 */
QSlider::groove:horizontal {{ height: 5px; background: {CTRL}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {ACCENT}; width: 14px; margin: -5px 0; border-radius: 7px;
}}
QSlider::sub-page:horizontal {{ background: {ACCENT_DOWN}; border-radius: 2px; }}

/* 状态栏 / 分隔线 / 滚动条 */
QStatusBar {{ background: {PANEL}; color: {TEXT_DIM}; }}
QStatusBar::item {{ border: none; }}
QFrame[frameShape="5"] {{ color: {BORDER}; }}  /* VLine */
QScrollBar:vertical {{ background: {BG}; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QLabel {{ background: transparent; }}

/* 分屏 PanelHost */
QFrame#PanelHost {{
    background-color: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 6px;
}}
QLabel#PanelHostTitle {{
    color: {TEXT};
    font-weight: bold;
    padding-left: 2px;
}}
QPushButton#PanelHostBtn {{
    padding: 2px 8px;
    font-size: 12px;
    min-height: 22px;
}}
QLabel#PanelHostPlaceholder {{
    color: {TEXT_DIM};
    background: {BG};
}}
QSplitter::handle {{
    background: {BORDER};
}}
QSplitter::handle:horizontal {{ width: 3px; }}
QSplitter::handle:vertical {{ height: 3px; }}
"""


def apply_theme(app) -> None:
    """对 QApplication 应用深色主题。"""
    app.setStyleSheet(DARK_QSS)

