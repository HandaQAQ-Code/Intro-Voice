@echo off
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python -u export_personal.py
if errorlevel 1 (
    echo Model export failed or training has not completed. See the error above.
    pause
    exit /b 1
)
call start.bat --personal
