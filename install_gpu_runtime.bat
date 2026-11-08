@echo off
setlocal
cd /d "%~dp0"
py -3.11 -m pip install --no-deps --target data/runtime/onnx-cuda onnxruntime-gpu==1.23.2
if errorlevel 1 pause
