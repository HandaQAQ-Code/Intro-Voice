# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：把 mixer_service（Python 音频引擎）打成单文件 exe。

- 单文件（onefile），代码与依赖全部封进 exe，不暴露 .py 源码；
- 排除 torch（GPU 档延迟导入，体积 ~2.5GB，运行时再装）；
- 收集 PortAudio / libsndfile / soxr 等原生 DLL；
- 数据、模型、VST3 宿主 DLL、手机 APK 仍在 exe 同级目录（app_paths 解析）。
"""
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, collect_dynamic_libs

block_cipher = None

# 声音设备、音频文件、重采样的原生库 + 隐藏导入
hiddenimports = [
    "segno",
    "onnxruntime",
    "onnxruntime.capi._pybind_state",
    "soundfile",
    "soxr",
    "_sounddevice_data",
    "voxlock",
    "dpdfnet",
    "scipy.signal",
    "scipy.io",
    "numpy",
    "vst3_scan_worker",
    "app_paths",
    "mobile_mic",
    "virtual_device",
    "workshop_service",
    "market_service",
    "mixer_dsp",
    "obs_audio",
    "live_voice",
    "super_resolution",
]
hiddenimports += collect_submodules("onnxruntime")
hiddenimports += collect_submodules("voxlock")
hiddenimports += collect_submodules("dpdfnet")

# 收集二进制与数据文件
datas = []
datas += collect_data_files("_sounddevice_data")
datas += collect_dynamic_libs("soundfile")
datas += collect_dynamic_libs("soxr")
datas += collect_dynamic_libs("onnxruntime")
datas += collect_dynamic_libs("scipy")

a = Analysis(
    ["../mixer_service.py"],
    pathex=[".."],
    binaries=datas,
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "torch", "torchaudio", "torchvision",
        "tkinter", "matplotlib", "pandas", "PIL",
        "IPython", "jupyter", "notebook",
        "pytest", "setuptools", "pip",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="mixer_service",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # 无控制台窗口，服务静默运行
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
