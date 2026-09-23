@echo off
REM Build ADI Series Volume Controller as a folder ("mode dossier" / --onedir).
REM Run this from the project folder, in the venv where you have already
REM done:  pip install pyinstaller broadlink
REM
REM Expects, next to this script (i.e. next to rme_app.py):
REM   sendmidi.exe, receivemidi.exe    (from https://github.com/gbevin/SendMIDI
REM                                      / ReceiveMIDI releases)
REM   dist_icons\tray.ico, dist_icons\app.ico   (already in this project)
REM
REM Result: dist\ADI Series Volume Controller\  ready to zip and copy to another PC -
REM everything it needs is in that one folder, nothing else to install there.

setlocal enabledelayedexpansion
cd /d "%~dp0"

if not exist "sendmidi.exe" (
    echo [build] ERROR: sendmidi.exe not found next to build.bat.
    echo [build] Download it from https://github.com/gbevin/SendMIDI/releases and place it here.
    exit /b 1
)
if not exist "receivemidi.exe" (
    echo [build] ERROR: receivemidi.exe not found next to build.bat.
    echo [build] Download it from https://github.com/gbevin/ReceiveMIDI/releases and place it here.
    exit /b 1
)
if not exist "dist_icons\tray.ico" (
    echo [build] ERROR: dist_icons\tray.ico not found.
    exit /b 1
)
if not exist "dist_icons\app.ico" (
    echo [build] ERROR: dist_icons\app.ico not found.
    exit /b 1
)

echo [build] Cleaning previous build...
if exist "build" rmdir /s /q "build"
if exist "dist\ADI Series Volume Controller" rmdir /s /q "dist\ADI Series Volume Controller"

echo [build] Running PyInstaller (mode dossier)...
pyinstaller build.spec
if errorlevel 1 (
    echo [build] PyInstaller failed - see above.
    exit /b 1
)

set "OUT=dist\ADI Series Volume Controller"

echo [build] Copying sendmidi.exe / receivemidi.exe / tray.ico into "%OUT%"...
copy /y "sendmidi.exe" "%OUT%\" >nul
copy /y "receivemidi.exe" "%OUT%\" >nul
copy /y "dist_icons\tray.ico" "%OUT%\" >nul

echo.
echo [build] Done: "%OUT%"
echo [build] config.ini is created automatically on first launch (default
echo [build] layout) - copy your real config.ini into that folder if you
echo [build] want to keep your existing hotkeys/settings instead.