@echo off
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python -u train_voice.py %*
if errorlevel 1 pause
