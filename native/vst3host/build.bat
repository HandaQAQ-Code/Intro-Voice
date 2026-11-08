@echo off
setlocal enabledelayedexpansion
rem Build the native VST3 hosting DLL with the installed MSVC toolchain.
rem Usage: build.bat  (from native\vst3host or any directory)

set "HERE=%~dp0"
for %%I in ("%HERE%..\..") do set "ROOT=%%~fI"
pushd "%ROOT%"

set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" set "VSWHERE=%ProgramFiles%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" (
  echo [ERROR] vswhere.exe not found; install Visual Studio with C++ tools.
  exit /b 1
)
set "VSROOT="
for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSROOT=%%i"
if not defined VSROOT (
  echo [ERROR] No Visual Studio installation with MSVC C++ tools found.
  exit /b 1
)
call "%VSROOT%\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 (
  echo [ERROR] vcvars64.bat failed.
  exit /b 1
)

if not exist "native\bin" mkdir "native\bin"

set "SDK=third_party\vst3sdk"
set "SRC=native\vst3host\src\host.cpp"
set "SDKSRC=%SDK%\pluginterfaces\base\funknown.cpp %SDK%\pluginterfaces\base\coreiids.cpp %SDK%\pluginterfaces\base\conststringtable.cpp %SDK%\public.sdk\source\vst\vstinitiids.cpp %SDK%\public.sdk\source\common\commonstringconvert.cpp %SDK%\public.sdk\source\common\threadchecker_win32.cpp %SDK%\base\thread\source\flock.cpp %SDK%\public.sdk\source\common\commoniids.cpp %SDK%\public.sdk\source\common\memorystream.cpp %SDK%\public.sdk\source\common\pluginview.cpp %SDK%\public.sdk\source\vst\hosting\module.cpp %SDK%\public.sdk\source\vst\hosting\module_win32.cpp %SDK%\public.sdk\source\vst\hosting\plugprovider.cpp %SDK%\public.sdk\source\vst\hosting\connectionproxy.cpp %SDK%\public.sdk\source\vst\hosting\hostclasses.cpp %SDK%\public.sdk\source\vst\hosting\pluginterfacesupport.cpp %SDK%\public.sdk\source\vst\hosting\parameterchanges.cpp %SDK%\public.sdk\source\vst\utility\stringconvert.cpp %SDK%\base\source\baseiids.cpp %SDK%\base\source\fobject.cpp %SDK%\base\source\fstring.cpp"

cl /nologo /std:c++17 /O2 /EHsc /MD /W3 /utf-8 /DRELEASE=1 /D_CRT_SECURE_NO_WARNINGS ^
  /I "%SDK%" /I "native\vst3host\include" ^
  %SRC% %SDKSRC% ^
  /Fo:native\bin\ /Fd:native\bin\vst3host.pdb ^
  /link /DLL /OUT:native\bin\vst3host.dll ^
  ole32.lib shell32.lib user32.lib gdi32.lib advapi32.lib
set "RC=%errorlevel%"
popd
if not "%RC%"=="0" (
  echo [ERROR] Build failed.
  exit /b %RC%
)
echo [OK] native\bin\vst3host.dll
