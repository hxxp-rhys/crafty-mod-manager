# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec - single-file windowed Windows executable.

    python -m PyInstaller CraftyModManager.spec --noconfirm --clean
"""
import os
import sys
from pathlib import Path

HERE = Path(os.getcwd())

datas = []
icon_png = HERE / "assets" / "icon.png"
icon_ico = HERE / "assets" / "icon.ico"
if icon_png.exists():
    datas.append((str(icon_png), "assets"))

hiddenimports = [
    # keyring finds its backends by entry point; PyInstaller needs them named.
    "keyring.backends.Windows",
    "keyring.backends.SecretService",
    "keyring.backends.chainer",
    "keyring.backends.fail",
    "keyring.backends.null",
    # paramiko pulls these lazily
    "paramiko.ed25519key",
    "cryptography.hazmat.backends.openssl",
]

# Qt modules we never touch - dropping them roughly halves the binary.
excludes = [
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DAnimation",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtBluetooth",
    "PySide6.QtPositioning", "PySide6.QtSensors", "PySide6.QtSerialPort",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtTest", "PySide6.QtSql",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "tkinter", "matplotlib", "numpy", "pandas", "PIL", "scipy", "pytest",
]

a = Analysis(
    ["app.py"],
    pathex=[str(HERE)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="CraftyModManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=["vcruntime140.dll", "python3.dll", "Qt6Core.dll", "Qt6Gui.dll"],
    runtime_tmpdir=None,
    console=False,          # windowed app - no console flash
    disable_windowed_traceback=False,
    icon=str(icon_ico) if icon_ico.exists() else None,
)
