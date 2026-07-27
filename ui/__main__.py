"""python -m ui 入口：先预加载 SDK，再启动主窗口。"""

try:
    import elite_cs_sdk  # noqa: F401
except ImportError:
    pass

from ui.main_window import main

if __name__ == "__main__":
    main()
