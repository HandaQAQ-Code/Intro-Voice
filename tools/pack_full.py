"""Build the portable "完整版" folder: WinUI app + bundled Python + VST3 host + data.

Usage:  .venv\\Scripts\\python.exe tools\\pack_full.py
The output folder is RecordOut_Full\\ and can be copied to any Windows x64 PC.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "RecordOut_Full"
BUILD = ROOT / "MixerUI" / "bin" / "x64" / "Release" / "net8.0-windows10.0.19041.0" / "win-x64"
VENV_SITE = ROOT / ".venv" / "Lib" / "site-packages"
BASE_PY = Path(sys.base_prefix)

SCRIPTS = ["mixer_service.py", "live_voice.py", "obs_audio.py", "super_resolution.py",
           "gpu_super_resolution.py", "gpu_extractor.py", "gpu_live.py",
           "vst3_scan_worker.py", "workshop_service.py", "market_service.py",
           "mobile_mic.py"]
STDLIB_SKIP = {"site-packages", "test", "idlelib", "tkinter", "turtledemo", "lib2to3"}
DATA_FILES = [
    ("data/target.wav", "data/target.wav"),
    ("data/settings.json", "data/settings.json"),
    ("data/models/personal48k/model.onnx", "data/models/personal48k/model.onnx"),
    ("data/models/personal48k/model.json", "data/models/personal48k/model.json"),
    ("data/super_resolution/flashsr.onnx", "data/super_resolution/flashsr.onnx"),
    ("data/super_resolution/flashsr_github.onnx", "data/super_resolution/flashsr_github.onnx"),
    ("data/super_resolution/flashsr_lite.onnx", "data/super_resolution/flashsr_lite.onnx"),
    ("data/super_resolution/flashsr_source.json", "data/super_resolution/flashsr_source.json"),
    ("data/super_resolution/lavasr/config.yaml", "data/super_resolution/lavasr/config.yaml"),
    ("data/super_resolution/lavasr/pytorch_model.bin", "data/super_resolution/lavasr/pytorch_model.bin"),
]
# Models that the libraries normally download to the user profile. They are
# copied here so the portable build runs offline.
DPDFNET_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "dpdfnet" / "models"
VOXLOCK_DIR = Path(os.environ.get("USERPROFILE", "")) / ".cache" / "huggingface" / "hub" / \
    "models--arbazsiddiqui--voxlock"


def log(text: str) -> None:
    print(text, flush=True)


def copy_tree(source: Path, target: Path) -> int:
    """Copies a tree and returns the number of files written."""
    if not source.is_dir():
        raise SystemExit(f"缺少目录：{source}")
    target.mkdir(parents=True, exist_ok=True)
    count = 0
    for entry in source.iterdir():
        if entry.name.startswith(".") or entry.name == "__pycache__":
            continue
        if entry.is_dir():
            count += copy_tree(entry, target / entry.name)
        else:
            shutil.copyfile(entry, target / entry.name)
            count += 1
    return count


def copy_stdlib(source: Path, target: Path) -> int:
    target.mkdir(parents=True, exist_ok=True)
    count = 0
    for entry in source.iterdir():
        if entry.name in STDLIB_SKIP or entry.name.startswith("."):
            continue
        if entry.is_dir():
            count += copy_tree(entry, target / entry.name)
        else:
            shutil.copyfile(entry, target / entry.name)
            count += 1
    return count


def main() -> int:
    if not BUILD.is_dir():
        raise SystemExit("请先构建 WinUI：dotnet build MixerUI\\MixerUI.csproj -c Release -p:Platform=x64")
    if not BASE_PY.is_dir():
        raise SystemExit(f"找不到 Python 运行时：{BASE_PY}")

    if DEST.exists():
        log(f"清理旧目录 {DEST.name}…")
        shutil.rmtree(DEST)
    DEST.mkdir(parents=True)

    log("1/6 复制 WinUI 前端…")
    copy_tree(BUILD, DEST / "RecordOutMixer")

    log("2/6 复制 Python 标准库…")
    runtime = DEST / "runtime"
    runtime.mkdir()
    for name in ("python.exe", "pythonw.exe", "python311.dll",
                 "vcruntime140.dll", "vcruntime140_1.dll"):
        source = BASE_PY / name
        if source.is_file():
            shutil.copyfile(source, runtime / name)
    copy_stdlib(BASE_PY / "DLLs", runtime / "DLLs")
    copy_stdlib(BASE_PY / "Lib", runtime / "Lib")

    log("3/6 复制第三方依赖（numpy / onnxruntime / 声纹 / 超分…）…")
    site = runtime / "Lib" / "site-packages"
    site.mkdir()
    count = 0
    for entry in VENV_SITE.iterdir():
        if entry.name.startswith("~") or entry.name == "__pycache__":
            continue
        if entry.is_dir():
            count += copy_tree(entry, site / entry.name)
        else:
            shutil.copyfile(entry, site / entry.name)
            count += 1
    log(f"    已复制 {count} 个文件")

    log("4/6 复制宿主 DLL、脚本与模型…")
    (DEST / "native" / "bin").mkdir(parents=True)
    shutil.copyfile(ROOT / "native" / "bin" / "vst3host.dll", DEST / "native" / "bin" / "vst3host.dll")
    for name in SCRIPTS:
        shutil.copyfile(ROOT / name, DEST / name)
    for relative, destination in DATA_FILES:
        source = ROOT / relative
        if not source.is_file():
            log(f"    跳过缺失文件 {relative}")
            continue
        target = DEST / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    (DEST / "data" / "mixer" / "presets").mkdir(parents=True, exist_ok=True)
    # 插件市场离线兜底目录与手机端安装包
    if (ROOT / "plugin-market").is_dir():
        copy_tree(ROOT / "plugin-market", DEST / "plugin-market")
    apk = ROOT / "mobile" / "IntroVoiceMic.apk"
    if apk.is_file():
        (DEST / "mobile").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(apk, DEST / "mobile" / "IntroVoiceMic.apk")
        log(f"    已内置手机端安装包（{apk.stat().st_size / 1024 / 1024:.1f} MB）")
    if (DPDFNET_DIR / "dpdfnet2.onnx").is_file():
        target = DEST / "data" / "models" / "dpdfnet"
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(DPDFNET_DIR / "dpdfnet2.onnx", target / "dpdfnet2.onnx")
        log("    已内置降噪模型 dpdfnet2.onnx")
    if VOXLOCK_DIR.is_dir():
        copy_tree(VOXLOCK_DIR, DEST / "data" / "runtime" / "hf" / "hub" / VOXLOCK_DIR.name)
        log("    已内置声纹嵌入模型（voxlock）")
    else:
        log("    警告：未找到 voxlock 模型缓存，首次使用 AI 时会联网下载")

    log("5/6 安装 pip 到内置 Python（供 GPU 支持安装使用）…")
    subprocess.run([str(runtime / "python.exe"), "-m", "ensurepip", "--upgrade"],
                   check=False, capture_output=True, cwd=str(DEST))
    check = (
        "import numpy, scipy, sounddevice, soxr, onnxruntime, voxlock, dpdfnet\n"
        "from dpdfnet.models import resolve_model\n"
        "assert resolve_model(model='dpdfnet2', auto_download=False).onnx_path.is_file()\n"
    )
    env = {**os.environ, "PYTHONUTF8": "1",
           "DPDFNET_MODEL_DIR": str(DEST / "data" / "models" / "dpdfnet"),
           "HF_HOME": str(DEST / "data" / "runtime" / "hf")}
    subprocess.run([str(runtime / "python.exe"), "-c", check],
                   check=True, cwd=str(DEST), env=env)
    log("    内置解释器自检通过（依赖与离线模型均可解析）")

    log("6/6 写入启动脚本与说明…")
    write_launchers()
    total = sum(f.stat().st_size for f in DEST.rglob("*") if f.is_file())
    log(f"完成：{DEST}（{total / (1024 ** 3):.2f} GB）")
    return 0


def write_launchers() -> None:
    (DEST / "启动调音台.bat").write_text(
        "@echo off\r\n"
        "cd /d \"%~dp0\"\r\n"
        "start \"\" \"RecordOutMixer\\RecordOutMixer.exe\"\r\n",
        encoding="utf-8")
    (DEST / "start_mixer.bat").write_text(
        "@echo off\r\n"
        "cd /d \"%~dp0\"\r\n"
        "start \"\" \"RecordOutMixer\\RecordOutMixer.exe\"\r\n",
        encoding="utf-8")
    tools = DEST / "tools"
    tools.mkdir(exist_ok=True)
    (tools / "install_gpu_support.bat").write_text(
        "@echo off\r\n"
        "cd /d \"%~dp0..\"\r\n"
        "echo Installing CUDA PyTorch into the bundled Python (about 2.5 GB download)...\r\n"
        "runtime\\python.exe -m pip install --no-warn-script-location "
        "torch==2.5.1+cu121 torchaudio==2.5.1+cu121 "
        "--index-url https://download.pytorch.org/whl/cu121\r\n"
        "if errorlevel 1 goto fail\r\n"
        "runtime\\python.exe -m pip install --no-warn-script-location "
        "vocos==0.1.0 einops pyyaml encodec==0.1.1\r\n"
        "if errorlevel 1 goto fail\r\n"
        "echo GPU tier is ready. Restart the mixer and pick the GPU mode.\r\n"
        "pause\r\n"
        "exit /b 0\r\n"
        ":fail\r\n"
        "echo Install failed. Check the network and run this script again.\r\n"
        "pause\r\n"
        "exit /b 1\r\n",
        encoding="utf-8")
    (DEST / "README.txt").write_text(README, encoding="utf-8")


README = """Record Out 直播调音台 · 完整版（免安装）

启动：双击 启动调音台.bat（或 start_mixer.bat）。

程序结构
  RecordOutMixer\\       WinUI 3 前端（自带 .NET 8 与 Windows App SDK 运行时）
  runtime\\              内置 Python 3.11，已包含 numpy / onnxruntime / 声纹 / 超分依赖
  native\\bin\\          原生 VST3 宿主 vst3host.dll
  data\\                 声纹 target.wav、个人模型、FlashSR / LavaSR 超分模型、预设
  *.py                  Python 音频引擎（mixer_service.py 等）

使用步骤
  1. 物理麦克风选择你的麦克风；输出选择 CABLE Input（VB-Audio Virtual Cable）。
  2. 点“启动”，再点“扫描 / 添加”加载 VST3 效果器（Ozone、Pro-Q 等均可）。
  3. OBS 中从 CABLE Output 采集本路音频。

三档音质对比
  关闭（48 kHz 全带宽直通）
     完全不经过 AI，音质 = 原声，CPU 占用 ≈0%，延迟最低。缺点是噪声、房间混响原样输出。
   CPU（声纹 + 降噪 + FlashSR 超分）
     明显更干净，16 kHz 人声重建回 48 kHz；音质自然耐听，但高频细节略平滑。
     占用约 1~2 个 CPU 核心，不需要显卡。
   GPU（声纹 + 降噪 + LavaSR v2 超分）
     音质最好：齿音、气声、高频纹理最接近真实，几乎听不出重建痕迹；显存约 1~2 GB。
     首次使用需运行 tools\\install_gpu_support.bat 安装 CUDA 版 PyTorch（约 2.5 GB）。

超分延迟档
  低延迟 fast   192 ms 前瞻：连麦更跟手，瞬态细节略少
  平衡  balanced 224 ms 前瞻：音质与延迟折中，日常推荐
  完整  quality  352 ms 前瞻：超分最稳最自然，适合录制

性能监控
  主界面实时显示 CPU / GPU / 内存占用、RTF 实时系数、丢块与欠载计数、插件延迟。

日志
  data\\mixer\\service.log（引擎）、data\\mixer\\ui.log（界面）

说明
  VST3 宿主基于 Steinberg VST3 SDK（MIT 许可），随本程序一起分发。
  VB-Audio Virtual Cable 为第三方软件，需自行安装。
"""


if __name__ == "__main__":
    sys.exit(main())
