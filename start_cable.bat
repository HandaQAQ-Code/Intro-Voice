@echo off
setlocal
cd /d "%~dp0"
call start_gpu.bat --cable %*
