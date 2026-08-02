# PyInstaller runtime hook：必须在 pyi_rth_pyqt5 之前执行。
# Windows 上若先加载 Qt 再 import elite_cs_sdk，进程会 0xC0000005 静默退出。
try:
    import elite_cs_sdk  # noqa: F401
except Exception:
    pass
