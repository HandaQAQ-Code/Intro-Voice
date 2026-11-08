@echo off
rem Record Out Mixer launcher
cd /d "%~dp0"

set "EXE=MixerUI\bin\x64\Release\net8.0-windows10.0.19041.0\win-x64\RecordOutMixer.exe"

if exist "%EXE%" goto run
echo Building WinUI front-end for the first time...
dotnet build MixerUI\MixerUI.csproj -c Release -p:Platform=x64
if errorlevel 1 goto fail

:run
if not exist "native\bin\vst3host.dll" call native\vst3host\build.bat
start "" "%EXE%"
exit /b 0

:fail
echo Build failed. Please install the .NET 8 SDK.
pause
exit /b 1
