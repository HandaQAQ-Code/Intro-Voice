@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python training_status.py %*
pause
