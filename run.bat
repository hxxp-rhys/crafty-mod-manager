@echo off
setlocal enabledelayedexpansion
title Crafty Mod Manager

rem ---------------------------------------------------------------------------
rem  Creates a private virtual environment on first run, installs dependencies,
rem  then launches the app. Safe to run every time - it only does work when
rem  something is missing.
rem ---------------------------------------------------------------------------

cd /d "%~dp0"

set "VENV=.venv"
set "PY=%VENV%\Scripts\python.exe"
set "STAMP=%VENV%\.deps-installed"

rem --- find a usable Python -------------------------------------------------
where py >nul 2>&1
if %errorlevel%==0 (
    set "LAUNCHER=py -3"
) else (
    where python >nul 2>&1
    if !errorlevel!==0 (
        set "LAUNCHER=python"
    ) else (
        echo.
        echo   Python 3 was not found on this PC.
        echo.
        echo   Install it from https://www.python.org/downloads/windows/
        echo   and tick "Add python.exe to PATH" during setup, then run this again.
        echo.
        pause
        exit /b 1
    )
)

rem --- check the version is at least 3.10 -----------------------------------
%LAUNCHER% -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Python 3.10 or newer is required.
    %LAUNCHER% --version
    echo.
    pause
    exit /b 1
)

rem --- create the venv ------------------------------------------------------
if not exist "%PY%" (
    echo Creating a virtual environment in %VENV% ...
    %LAUNCHER% -m venv "%VENV%"
    if errorlevel 1 (
        echo Failed to create the virtual environment.
        pause
        exit /b 1
    )
)

rem --- install dependencies once --------------------------------------------
if not exist "%STAMP%" (
    echo Installing dependencies - this only happens once and takes a minute...
    "%PY%" -m pip install --upgrade pip --quiet
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo   Dependency installation failed. Check your internet connection
        echo   and any proxy settings, then run this file again.
        echo.
        pause
        exit /b 1
    )
    echo installed > "%STAMP%"
)

rem --- go -------------------------------------------------------------------
"%PY%" app.py %*
set "RC=%errorlevel%"
if not "%RC%"=="0" (
    echo.
    echo   The app exited with code %RC%.
    echo   The log file is in  %%APPDATA%%\CraftyModManager\craftymm.log
    echo.
    pause
)
endlocal
exit /b %RC%
