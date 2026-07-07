@echo off
rem ============================================================
rem  競馬 予想チャットくん  exeビルド(ワンコマンド)
rem  このバッチをダブルクリック、または「build_exe.bat」と実行するだけで
rem  dist\競馬予想チャットくん.exe が作られます。
rem ============================================================
setlocal
cd /d %~dp0

echo.
echo ============================================================
echo   競馬 予想チャットくん  exe ビルドを開始します
echo ============================================================
echo.

rem --- Python の確認 ---
where python >nul 2>&1
if errorlevel 1 (
    echo [エラー] python が見つかりません。
    echo   https://www.python.org/ からPythonをインストールし、
    echo   インストール時に「Add Python to PATH」にチェックを入れてください。
    pause
    exit /b 1
)

rem --- PyInstaller が無ければ自動インストール ---
python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo PyInstaller をインストールします...
    python -m pip install --upgrade pip
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo [エラー] PyInstaller のインストールに失敗しました。
        pause
        exit /b 1
    )
)

rem --- ビルド実行 ---
echo ビルド中です。数分かかることがあります...
python -m PyInstaller keiba_yosou.spec --noconfirm --clean
if errorlevel 1 (
    echo [エラー] ビルドに失敗しました。上のログを確認してください。
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   完成しました!
echo   dist\競馬予想チャットくん.exe をダブルクリックで起動できます。
echo ============================================================
echo.
echo   ※exe と同じ階層に horselist フォルダ等を置くと、そこに
echo     CSV・投票リスト・keiba_params.json などが保存されます。
echo.
pause
