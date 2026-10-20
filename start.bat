@echo off
rem Double-click to run the GreenGate3D Lead Finder.
rem First run: creates a private Python environment (.venv), installs the
rem packages, and makes a .env for your API keys. Later runs just start the app.
setlocal
cd /d "%~dp0"
title GreenGate3D Lead Finder

rem --- Find Python (the "py" launcher works even when Python isn't on PATH) ---
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY (
    echo Python is not installed.
    echo.
    echo Install Python 3.12 from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" on the first installer screen.
    echo Then double-click start.bat again.
    goto :fail
)

rem --- One-time setup: private environment + packages ---
rem The marker file is written only after a successful install. Without it
rem the environment is rebuilt from scratch, so a half-finished setup never
rem gets stuck.
if not exist ".venv\installed.txt" (
    echo Setting up for the first time. This takes a few minutes...
    if exist ".venv" rmdir /s /q ".venv"
    %PY% -m venv .venv || goto :setup_failed
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :setup_failed
    echo ok> ".venv\installed.txt"
)

rem --- API keys ---
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo.
    echo A file called .env has opened in Notepad.
    echo Paste your API keys after the = signs, save it, close Notepad,
    echo then double-click start.bat again.
    start "" notepad ".env"
    goto :fail
)

echo Starting... your browser will open shortly.
echo Keep this window open while you use the app. Close it to stop.
echo.
".venv\Scripts\python.exe" app.py
if errorlevel 1 goto :fail
exit /b 0

:setup_failed
echo.
echo Setup failed. Check your internet connection, then run start.bat again.

:fail
echo.
pause
exit /b 1
