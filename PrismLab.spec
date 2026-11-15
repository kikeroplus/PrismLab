# -*- mode: python ; coding: utf-8 -*-
# EXE を作る:  python -m PyInstaller PrismLab.spec --noconfirm   → dist\PrismLab.exe
# アイコンを作り直す:  python tools\make_ico.py
# GenCommon は同じ AI_projects フォルダに並べて置いてあるものを取り込む (ImageMimicArt と同じ)。


a = Analysis(
    ['main.py'],
    pathex=['..\\GenCommon'],
    binaries=[],
    datas=[],
    # ★の読み書き (rating.py) は関数の中で読み込むので、名前を挙げておく
    hiddenimports=['pythoncom', 'pywintypes', 'win32com.propsys', 'win32com.propsys.propsys',
                   'win32com.propsys.pscon'],
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
    a.binaries,
    a.datas,
    [],
    name='PrismLab',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets\\icon.ico'],
)
