# PyInstaller 打包（可选）
# pyinstaller RobotControllerGUI.spec

block_cipher = None

a = Analysis(
    ['ui/main_window.py'],
    pathex=['.'],
    binaries=[],
    datas=[('config/devices.yaml', 'config'), ('docs', 'docs')],
    hiddenimports=['devices', 'core', 'tasks', 'ui'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
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
