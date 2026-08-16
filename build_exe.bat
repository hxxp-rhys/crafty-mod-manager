@echo off
setlocal
title Build Crafty Mod Manager .exe

rem ---------------------------------------------------------------------------
rem  Produces dist\CraftyModManager.exe - a single-file Windows executable.
rem  Run this once on your PC; it takes a few minutes and needs ~1 GB free.
rem ---------------------------------------------------------------------------

cd /d "%~dp0"

set "VENV=.venv"
set "PY=%VENV%\Scripts\python.exe"

if not exist "%PY%" (
    echo No virtual environment yet - running run.bat once to create it.
    echo Close the app when it opens, then this build will continue.
    call run.bat
)

if not exist "%PY%" (
    echo Could not create the virtual environment. Run run.bat first.
    pause
    exit /b 1
)

echo Installing build tools...
"%PY%" -m pip install --upgrade pip --quiet
"%PY%" -m pip install -r requirements-dev.txt
if errorlevel 1 (
    echo Failed to install PyInstaller.
    pause
    exit /b 1
)

echo.
echo Cleaning previous builds...
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist

echo.
echo Building - this takes a few minutes...
"%PY%" -m PyInstaller CraftyModManager.spec --noconfirm --clean
if errorlevel 1 (
    echo.
    echo   Build failed. Scroll up for the PyInstaller error.
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Done.  dist\CraftyModManager.exe
echo.
echo   That single file is the whole app - copy it anywhere.
echo   Settings live in %%APPDATA%%\CraftyModManager
echo ============================================================
echo.
pause
endlocal
