@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3.11 -m venv .venv
    if errorlevel 1 (
        echo Please install Python 3.11, including Tcl/Tk, then run again.
        pause
        exit /b 1
    )
)
if not exist ".venv\.live-voice-ready-v1" (
    ".venv\Scripts\python.exe" -m ensurepip --upgrade
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Dependency installation failed. Check your network and retry.
        pause
        exit /b 1
    )
    type nul > ".venv\.live-voice-ready-v1"
)
".venv\Scripts\python.exe" live_voice.py %*
if errorlevel 1 pause
