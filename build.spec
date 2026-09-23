# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for ADI Series Volume Controller - "mode dossier" (--onedir), not
# --onefile: one folder with the exe and its files next to it, faster to
# start and easier to drop sendmidi.exe/receivemidi.exe/tray.ico into.
#
# Run on Windows, in the venv where `pip install broadlink` (and
# pyinstaller) was done:
#       pyinstaller build.spec
# (or just run build.bat, which does this and also copies sendmidi.exe /
# receivemidi.exe / tray.ico into dist\ADI Series Volume Controller\ afterwards -
# PyInstaller only bundles Python, not those two separate .exe tools)
#
# broadlink pulls in `cryptography` for its AES layer, which ships
# compiled (Rust) binary modules - the single package PyInstaller most
# often fails to fully collect. collect-all forces every submodule and
# binary of both packages in, rather than relying on static analysis to
# find broadlink's own runtime-conditional `import broadlink` correctly.
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []
for pkg in ("broadlink", "cryptography"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ['rme_app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ADI Series Volume Controller',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,               # tray app: no console window on launch
    icon='dist_icons/app.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='ADI Series Volume Controller',
)
