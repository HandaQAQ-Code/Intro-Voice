"""Build the portable "完整版" folder: WinUI app + 封装的 Python 引擎目录 + data.

Usage:  .venv\\Scripts\\python.exe tools\\pack_full.py

前置：先运行 PyInstaller 产出 onedir 引擎（见 tools/mixer_service.spec）：
    .venv\\Scripts\\python.exe -m PyInstaller --noconfirm --clean tools/mixer_service.spec
    产物为 dist\\mixer_service\\（含 mixer_service.exe + _internal\\）。

输出目录 RecordOut_Full\\，可复制到任意 Windows x64 PC。Python 引擎已封装为
onedir 目录（不暴露 .py 源码），data / native / mobile 等资源放在引擎目录旁。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "RecordOut_Full"
BUILD = ROOT / "MixerUI" / "bin" / "x64" / "Release" / "net8.0-windows10.0.19041.0" / "win-x64"

# onedir 引擎目录（由 PyInstaller 产出）。
ENGINE_DIR = ROOT / "dist" / "mixer_service"
# 引擎运行所需的运行时数据（模型、APK、预设），放在引擎目录内（app_paths.ROOT = 引擎目录）。
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
    if not (ENGINE_DIR / "mixer_service.exe").is_file():
        raise SystemExit(
            "缺少 dist\\mixer_service\\mixer_service.exe。请先运行：\n"
            "  .venv\\Scripts\\python.exe -m PyInstaller --noconfirm --clean tools/mixer_service.spec")

    if DEST.exists():
        log(f"清理旧目录 {DEST.name}…")
        shutil.rmtree(DEST)
    DEST.mkdir(parents=True)

    log("1/4 复制 WinUI 前端…")
    copy_tree(BUILD, DEST / "RecordOutMixer")

    log("2/4 复制封装的引擎目录（onedir）…")
    engine_target = DEST / "mixer_service"
    copy_tree(ENGINE_DIR, engine_target)

    log("3/4 复制原生宿主 DLL、模型与手机端安装包…")
    engine_root = engine_target
    (engine_root / "native" / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "native" / "bin" / "vst3host.dll",
                    engine_root / "native" / "bin" / "vst3host.dll")
    for relative, destination in DATA_FILES:
        source = ROOT / relative
        if not source.is_file():
            log(f"    跳过缺失文件 {relative}")
            continue
        target = engine_root / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    (engine_root / "data" / "mixer" / "presets").mkdir(parents=True, exist_ok=True)
    if (ROOT / "plugin-market").is_dir():
        copy_tree(ROOT / "plugin-market", engine_root / "plugin-market")
    apk = ROOT / "mobile" / "IntroVoiceMic.apk"
    if apk.is_file():
        (engine_root / "mobile").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(apk, engine_root / "mobile" / "IntroVoiceMic.apk")
        log(f"    已内置手机端安装包（{apk.stat().st_size / 1024 / 1024:.1f} MB）")
    if (DPDFNET_DIR / "dpdfnet2.onnx").is_file():
        target = engine_root / "data" / "models" / "dpdfnet"
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(DPDFNET_DIR / "dpdfnet2.onnx", target / "dpdfnet2.onnx")
        log("    已内置降噪模型 dpdfnet2.onnx")
    if VOXLOCK_DIR.is_dir():
        copy_tree(VOXLOCK_DIR, engine_root / "data" / "runtime" / "hf" / "hub" / VOXLOCK_DIR.name)
        log("    已内置声纹嵌入模型（voxlock）")
    else:
        log("    警告：未找到 voxlock 模型缓存，首次使用 AI 时会联网下载")

    log("4/4 写入启动脚本与说明…")
    write_launchers()
    total = sum(f.stat().st_size for f in DEST.rglob("*") if f.is_file())
    log(f"完成：{DEST}（{total / (1024 ** 3):.2f} GB）")
    return 0


LAUNCHER = (
    "@echo off\r\n"
    "rem Record Out Mixer - portable launcher\r\n"
    "title Record Out Mixer\r\n"
    "cd /d \"%~dp0\"\r\n"
    "\r\n"
    "if not exist \"RecordOutMixer\\RecordOutMixer.exe\" goto missing\r\n"
    "if not exist \"mixer_service\\mixer_service.exe\" goto broken\r\n"
    "\r\n"
    "start \"\" \"RecordOutMixer\\RecordOutMixer.exe\"\r\n"
    "exit /b 0\r\n"
    "\r\n"
    ":broken\r\n"
    "echo.\r\n"
    "echo   [ERROR] Incomplete package: mixer_service\\mixer_service.exe not found.\r\n"
    "echo   Please re-extract the whole folder.\r\n"
    "echo.\r\n"
    "pause\r\n"
    "exit /b 1\r\n"
    "\r\n"
    ":missing\r\n"
    "echo.\r\n"
    "echo   [ERROR] RecordOutMixer\\RecordOutMixer.exe not found.\r\n"
    "echo   Please re-extract the whole folder.\r\n"
    "echo.\r\n"
    "pause\r\n"
    "exit /b 1\r\n"
)


def write_launchers() -> None:
    for name in ("启动调音台.bat", "start_mixer.bat"):
        (DEST / name).write_text(LAUNCHER, encoding="utf-8")
    (DEST / "README.txt").write_text(README, encoding="utf-8")


README = """Record Out 直播调音台 · 完整版（免安装）

启动：双击 启动调音台.bat（或 start_mixer.bat）。

程序结构
  RecordOutMixer\\            WinUI 3 前端（自带 .NET 8 与 Windows App SDK 运行时）
  mixer_service\\             音频引擎（已封装，不含 Python 源码）
    mixer_service.exe        引擎主程序
    _internal\\               依赖（numpy / onnxruntime / 声纹 / 超分等）
    native\\bin\\vst3host.dll 原生 VST3 宿主
    data\\                    声纹、模型、超分、预设
    mobile\\IntroVoiceMic.apk 手机麦克风 Android 安装包
    gpu_packages\\            可选的 CUDA PyTorch（界面点「下载 GPU 支持」后生成）

使用步骤
  0. 第一次打开会自动弹出新手引导，一步步带你配好；侧边栏「新手教程」可随时重走。
     引导顺序：选麦克风（本机 / 手机当麦克风分支）→ 录声纹 → 安装 VB-CABLE
     → 选 CABLE Input → 下拉框选音质档 → 启动引擎 → OBS 采集。每步都会当场写进配置。
  1. 手动配置时：输入选你的麦克风；输出选 CABLE Input（VB-Audio Virtual Cable）。
  2. 点“启动”，再点“扫描 / 添加”加载 VST3 效果器（Ozone、Pro-Q 等均可）。
  3. OBS 中从 CABLE Output 采集本路音频。

三档音质对比
  关闭（48 kHz 全带宽直通）
     完全不经过 AI，音质 = 原声，CPU 占用 ≈0%，延迟最低。缺点是噪声、房间混响原样输出。
   CPU（声纹 + 降噪 + FlashSR 超分）
     明显更干净，16 kHz 人声重建回 48 kHz；音质自然耐听，但高频细节略平滑。
     占用约 1~2 个 CPU 核心，不需要显卡。
   GPU（声纹 + 降噪 + LavaSR v2 超分）
     主包默认不随附 torch 以瘦身。首次用 GPU 档时，在「主控 → AI 人声处理」点
     「下载 GPU 支持」，程序会用系统 Python 3.11 把 CUDA 版 PyTorch 装到
     mixer_service\\gpu_packages\\（约 2.5 GB），装完重启即可选 GPU 档。
     需 NVIDIA 显卡 + 已安装较新的驱动。

超分延迟档
  低延迟 fast   192 ms 前瞻：连麦更跟手，瞬态细节略少
  平衡  balanced 224 ms 前瞻：音质与延迟折中，日常推荐
  完整  quality  352 ms 前瞻：超分最稳最自然，适合录制

性能监控
  主界面实时显示 CPU / GPU / 内存占用、RTF 实时系数、丢块与欠载计数、插件延迟。

日志
  mixer_service\\data\\mixer\\service.log（引擎）、mixer_service\\data\\mixer\\ui.log（界面）

说明
  VST3 宿主基于 Steinberg VST3 SDK（MIT 许可），随本程序一起分发。
  VB-Audio Virtual Cable 为第三方软件，本包不包含，需自行安装。
  音频引擎已封装为 onedir 目录，不暴露源码。
"""


if __name__ == "__main__":
    sys.exit(main())
