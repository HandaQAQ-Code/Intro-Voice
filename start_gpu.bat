@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
    echo Please run start.bat once to install the audio dependencies.
    pause
    exit /b 1
)
py -3.11 gpu_live.py %*
if errorlevel 1 pause
