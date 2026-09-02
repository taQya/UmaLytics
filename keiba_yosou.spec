# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec: 競馬 予想チャットくん GUI を単一exe化する設定。
build_exe.bat から自動で使われます(手動なら: pyinstaller keiba_yosou.spec)。

GUIは実行時に fetch_jra / spat4_bet / tune_params / download_race_data /
keiba_yosou を動的import(または内部で参照)します。PyInstallerはこの
動的importを自動追跡できないため、hiddenimports に明示して同梱します。
"""

block_cipher = None

# GUIが動的に読み込むプロジェクト内モジュール
hidden = [
    "keiba_yosou",
    "fetch_jra",
    "spat4_bet",
    "tune_params",
    "download_race_data",
    "post_wordpress",
    "deba_table",
]

a = Analysis(
    ["keiba_yosou_gui.py"],
    pathex=["."],
    binaries=[],
    datas=[("keiba.ico", ".")],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # requests が無い環境でもビルドできるよう、未使用なら除外(download_race_data は
    # requests が無くても urllib で動くフォールバック実装済み)
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
    name="競馬予想チャットくん",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # GUIアプリなのでコンソール窓を出さない
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="keiba.ico",     # exeファイル自体のアイコン
)
