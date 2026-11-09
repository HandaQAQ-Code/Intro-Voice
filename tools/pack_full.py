"""Build the portable "完整版" folder: WinUI app + 封装好的 Python 引擎 exe + data.

Usage:  .venv\\Scripts\\python.exe tools\\pack_full.py

前置：先运行 PyInstaller 产出 mixer_service.exe（见 tools/mixer_service.spec）：
    .venv\\Scripts\\python.exe -m PyInstaller --noconfirm --clean tools/mixer_service.spec
    然后把 dist/mixer_service.exe 复制到项目根目录。

输出目录 RecordOut_Full\\，可复制到任意 Windows x64 PC。Python 引擎已封装为
单个 exe（不含 .py 源码），data / native / mobile 等资源仍放 exe 同级目录。
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

# 封装后的引擎：单个 exe，不含 .py 源码。由 PyInstaller 预先产出。
ENGINE_EXE = ROOT / "mixer_service.exe"
# 引擎运行所需的运行时数据（模型、APK、预设），仍放 exe 同级。
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
# 这些模型库默认下载到用户 profile，这里复制一份让便携版离线可用。
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


def main() -> int:
    if not BUILD.is_dir():
        raise SystemExit("请先构建 WinUI：dotnet build MixerUI\\MixerUI.csproj -c Release -p:Platform=x64")
    if not ENGINE_EXE.is_file():
        raise SystemExit(
            "缺少 mixer_service.exe。请先运行：\n"
            "  .venv\\Scripts\\python.exe -m PyInstaller --noconfirm --clean tools/mixer_service.spec\n"
            "然后把 dist/mixer_service.exe 复制到项目根目录。")

    if DEST.exists():
        log(f"清理旧目录 {DEST.name}…")
        shutil.rmtree(DEST)
    DEST.mkdir(parents=True)

    log("1/5 复制 WinUI 前端…")
    copy_tree(BUILD, DEST / "RecordOutMixer")

    log("2/5 复制封装后的引擎 exe…")
    shutil.copyfile(ENGINE_EXE, DEST / "mixer_service.exe")

    log("3/5 复制原生宿主 DLL、模型与手机端安装包…")
    (DEST / "native" / "bin").mkdir(parents=True)
    shutil.copyfile(ROOT / "native" / "bin" / "vst3host.dll", DEST / "native" / "bin" / "vst3host.dll")
    for relative, destination in DATA_FILES:
        source = ROOT / relative
        if not source.is_file():
            log(f"    跳过缺失文件 {relative}")
            continue
        target = DEST / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    (DEST / "data" / "mixer" / "presets").mkdir(parents=True, exist_ok=True)
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

    log("4/5 写入启动脚本与说明…")
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
        "echo GPU 档需要单独的 CUDA 环境，且当前封包版引擎（mixer_service.exe）不内置 torch。\r\n"
        "echo 如需 GPU 档，请使用源码版运行（未封包），或联系开发者获取 GPU 专属构建。\r\n"
        "pause\r\n",
        encoding="utf-8")
    (DEST / "README.txt").write_text(README, encoding="utf-8")


README = """Record Out 直播调音台 · 完整版（免安装）

启动：双击 启动调音台.bat（或 start_mixer.bat）。

程序结构
  RecordOutMixer\\       WinUI 3 前端（自带 .NET 8 与 Windows App SDK 运行时）
  mixer_service.exe     音频引擎（已封装为单个 exe，不含 Python 源码）
  native\\bin\\          原生 VST3 宿主 vst3host.dll
  data\\                 声纹 target.wav、个人模型、FlashSR / LavaSR 超分模型、预设
  mobile\\IntroVoiceMic.apk   手机麦克风 Android 安装包

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
     本封包版默认不含 torch，GPU 档会提示不可用；如需 GPU 档请使用源码版。

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
  音频引擎已封装为单文件 exe，不暴露源码。
"""


if __name__ == "__main__":
    sys.exit(main())
