# PyInstaller 打包（可选）
# pyinstaller --noconfirm --clean RobotControllerGUI.spec
#
# elite_cs_sdk 与 PyQt5 冲突：PyInstaller 在查找二进制依赖时会故意先 import Qt，
# 再 import elite_cs_sdk → 子进程 ACCESS_VIOLATION。
# 做法：主进程里单独收集 SDK 资源，并在 bindepend 阶段跳过对该包的 import。

from PyInstaller.utils.hooks import collect_all
import PyInstaller.building.build_main as _pyi_build_main

_elite_datas, _elite_binaries, _elite_hiddenimports = collect_all("elite_cs_sdk")

_orig_find_binary_dependencies = _pyi_build_main.find_binary_dependencies


def _find_binary_dependencies_skip_elite(binaries, collected_packages, *args, **kwargs):
    pkgs = [
        p for p in collected_packages
        if p != "elite_cs_sdk" and not str(p).startswith("elite_cs_sdk.")
    ]
    return _orig_find_binary_dependencies(binaries, pkgs, *args, **kwargs)


_pyi_build_main.find_binary_dependencies = _find_binary_dependencies_skip_elite

block_cipher = None

a = Analysis(
    ['ui/main_window.py'],
    pathex=['.'],
    binaries=_elite_binaries,
    datas=[
        ('config/devices.yaml', 'config'),
        ('config/devices.local.example.yaml', 'config'),
        ('config/rtsi', 'config/rtsi'),
        ('docs', 'docs'),
    ] + list(_elite_datas),
    hiddenimports=['devices', 'core', 'tasks', 'ui', 'elite_cs_sdk'] + list(_elite_hiddenimports),
    hookspath=[],
    hooksconfig={},
    # 自定义 rthook 先于 pyi_rth_pyqt5 执行，避免 Qt 抢先加载导致 exe 闪退
    runtime_hooks=['packaging/rthook_elite_first.py'],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='RobotControllerGUI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)
