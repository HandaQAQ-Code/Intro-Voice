# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：把 mixer_service（Python 音频引擎）打成 onedir 目录。

- onedir：产出一个目录（mixer_service.exe + _internal/），源码不暴露，
  启动快、可增量更新，比单文件 exe 更「散」；
- 排除 torch（GPU 档延迟导入，体积 ~2.5GB，用户可在界面点「下载」自行安装到
  独立的 gpu_packages/ 目录，引擎启动时追加到 sys.path 即可用）；
- 收集 PortAudio / libsndfile / soxr 等原生 DLL；
- 数据、模型、VST3 宿主 DLL、手机 APK 仍在 exe 同级目录（app_paths 解析）。
"""
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, collect_dynamic_libs

block_cipher = None

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
    "gpu_install",
]
hiddenimports += collect_submodules("onnxruntime")
hiddenimports += collect_submodules("voxlock")
hiddenimports += collect_submodules("dpdfnet")

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
    [],
    exclude_binaries=True,
    name="mixer_service",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 无控制台窗口，服务静默运行
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="mixer_service",
)
