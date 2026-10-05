# -*- mode: python ; coding: utf-8 -*-

# 把 C++ DLL（-c/--cpp 快速转换）连同 assets 一起打进 exe。
# Nuitka 版（scripts/build.txt）用 --include-package-data=ezbuild 即可；
# 这里给 PyInstaller 用，把 native DLL + 运行时映射打包到 _MEIPASS/ezbuild/native/。
import os

_spec_dir = os.path.dirname(os.path.abspath(__file__))
_native = os.path.join(_spec_dir, 'ezbuild', 'native')

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[(os.path.join(_native, 'water_structure_shared.dll'), 'ezbuild/native')],
    datas=[(os.path.join(_native, 'assets'), 'ezbuild/native/assets')],
    hiddenimports=['ezbuild.native', 'ezbuild.native._binding', 'ezbuild.world', 'PIL', 'PIL.Image'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='EzBuild',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='EzBuild',
)
