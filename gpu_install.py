"""GPU 档（CUDA PyTorch）按需下载与安装。

主包不随附 torch（约 2.5 GB）以瘦身。用户在主控页 GPU 档不可用时点
「下载 GPU 支持」，这里用系统 Python 3.11 的 pip 把依赖装到独立的
``gpu_packages/`` 目录；引擎启动时已把该目录加入 ``sys.path``，装完
重启即可 ``import torch``。

安装由子进程执行（frozen 引擎内没有 pip），进度与结果写到状态文件，
供界面轮询。所有状态文件放在 ``data/mixer/gpu/`` 下。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# 与引擎打包解释器一致的 Python 主版本；不匹配则装出的 .pyd 无法被 frozen 引擎导入。
REQUIRED_PY_MAJOR = 3
REQUIRED_PY_MINOR = 11

# CUDA 版 PyTorch + 音频编解码，与 requirements-gpu.txt 保持一致。
PACKAGES = [
    "torch==2.5.1+cu121",
    "torchaudio==2.5.1+cu121",
    "vocos==0.1.0",
    "einops",
    "pyyaml",
    "encodec==0.1.1",
]
INDEX_URL = "https://download.pytorch.org/whl/cu121"


def _run_kwargs(**extra) -> dict:
    """统一的 subprocess.run 参数：在无窗口（frozen console=False）环境下
    必须带 CREATE_NO_WINDOW，否则启动外部命令会报 0x800700e8。
    """
    kwargs: dict = {"capture_output": True, "text": True, "timeout": 30, "errors": "ignore"}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    kwargs.update(extra)
    return kwargs


def gpu_packages_dir(root: Path) -> Path:
    return root / "gpu_packages"


def status_dir(root: Path) -> Path:
    d = root / "data" / "mixer" / "gpu"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _status_path(root: Path) -> Path:
    return status_dir(root) / "install.json"


def _log_path(root: Path) -> Path:
    return status_dir(root) / "install.log"


def read_status(root: Path) -> dict:
    """读取安装状态；无状态文件时返回默认「未安装」。"""
    p = _status_path(root)
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    installed = (gpu_packages_dir(root) / "torch").is_dir()
    return {
        "state": "idle",           # idle | installing | done | error
        "installed": installed,
        "message": "已安装" if installed else "未安装",
        "running": False,
    }


def write_status(root: Path, state: str, message: str, running: bool,
                 installed: bool | None = None) -> None:
    data = read_status(root)
    data["state"] = state
    data["message"] = message
    data["running"] = running
    if installed is not None:
        data["installed"] = installed
    _status_path(root).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def find_python(root: Path, preferred: str | None = None) -> str | None:
    """找一个可用来装 torch 的 Python 3.11 解释器。

    优先用户手动指定的路径（preferred，来自设置），再试 exe 同级的
    runtime\python.exe（若发行包自带），然后 py -3.11，最后扫常见安装路径；
    版本必须是 3.11，否则装出的扩展无法被 frozen 引擎导入。
    """
    candidates: list[str] = []

    # 0) 用户手动指定的路径（最高优先）
    if preferred and Path(preferred).is_file():
        candidates.append(preferred)

    # 1) 发行包自带解释器（如果有）
    for name in ("python.exe", "pythonw.exe"):
        bundled = root / "runtime" / name
        if bundled.is_file():
            candidates.append(str(bundled))

    # 2) py launcher 的 3.11
    candidates.append("py")
    candidates.append("python")
    candidates.append("python3")

    # 3) 常见安装路径
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python"
    for ver in ("Python311", "Python312", "Python310"):
        exe = local / ver / "python.exe"
        if exe.is_file():
            candidates.append(str(exe))

    for cand in candidates:
        try:
            out = subprocess.run([cand, "-c",
                                  "import sys;print(sys.version_info[0],sys.version_info[1])"],
                                 **_run_kwargs(timeout=15))
        except (OSError, subprocess.SubprocessError):
            continue
        text = out.stdout.strip()
        parts = text.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            major, minor = int(parts[0]), int(parts[1])
            if major == REQUIRED_PY_MAJOR and minor == REQUIRED_PY_MINOR:
                return cand
    return None


def _python_matches(cand: str) -> tuple[str, str] | None:
    """返回 (major, minor) 若 cand 是 3.11，否则 None。"""
    try:
        out = subprocess.run([cand, "-c",
                              "import sys;print(sys.version_info[0],sys.version_info[1])"],
                             **_run_kwargs(timeout=15))
    except (OSError, subprocess.SubprocessError):
        return None
    parts = out.stdout.strip().split()
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        major, minor = int(parts[0]), int(parts[1])
        if major == REQUIRED_PY_MAJOR and minor == REQUIRED_PY_MINOR:
            return (str(major), str(minor))
    return None


def _native_candidates(root: Path, preferred: str | None = None) -> list[str]:
    """所有可能是 3.11 的候选解释器（与 find_python 同序，但不去重、全部返回）。"""
    candidates: list[str] = []
    if preferred and Path(preferred).is_file():
        candidates.append(preferred)
    for name in ("python.exe", "pythonw.exe"):
        bundled = root / "runtime" / name
        if bundled.is_file():
            candidates.append(str(bundled))
    candidates += ["py", "python", "python3"]
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python"
    for ver in ("Python311", "Python312", "Python310"):
        exe = local / ver / "python.exe"
        if exe.is_file():
            candidates.append(str(exe))
    # 去重保持顺序
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def detect_native_torch(root: Path, preferred: str | None = None) -> dict | None:
    """检测本机原生 Python 3.11 是否已装好可用的 CUDA 版 torch。

    优先用户手动指定的路径（preferred）。若找到，返回该解释器及其
    site-packages 路径（可直接复用，无需下载）；否则返回 None。
    这是「优先复用本机环境」的关键：原生通过就直接用原电脑的。
    """
    for cand in _native_candidates(root, preferred):
        if _python_matches(cand) is None:
            continue
        # 让解释器报告 torch 版本、CUDA 是否可用、torch 所在目录。
        # 用 torch.__file__ 的父目录（site-packages）定位，比 getsitepackages()[0] 更可靠。
        probe = (
            "import torch, os;"
            "print(torch.__version__);"
            "print('1' if torch.cuda.is_available() else '0');"
            "print(os.path.dirname(os.path.dirname(torch.__file__)))"
        )
        try:
            out = subprocess.run([cand, "-c", probe],
                                 **_run_kwargs(timeout=30))
        except (OSError, subprocess.SubprocessError):
            continue
        lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
        if len(lines) < 3:
            continue
        version, cuda_flag, site = lines[0], lines[1], lines[2]
        if cuda_flag == "1":
            return {
                "python": cand,
                "sitePackages": site,
                "torchVersion": version,
                "cuda": True,
            }
    return None


def _run_install(root: Path, python: str) -> None:
    target = gpu_packages_dir(root)
    target.mkdir(parents=True, exist_ok=True)
    log_path = _log_path(root)
    write_status(root, "installing", "正在下载并安装 CUDA 版 PyTorch（约 2.5 GB，耗时较长）…",
                 running=True, installed=False)

    cmd = [python, "-m", "pip", "install", "--no-warn-script-location",
           "--target", str(target), "--index-url", INDEX_URL] + PACKAGES
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        log.write(" ".join(cmd) + "\n")
        log.flush()
        # 无窗口环境必须 CREATE_NO_WINDOW + 重定向输出，否则 pip 报 0x800700e8。
        pip_kwargs = {"stdout": log, "stderr": subprocess.STDOUT,
                      "cwd": str(root), "timeout": 60 * 60}
        if os.name == "nt":
            pip_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.run(cmd, **pip_kwargs)
        except subprocess.TimeoutExpired:
            write_status(root, "error", "安装超时（超过 1 小时）。请检查网络后重试。",
                         running=False, installed=False)
            return
        except OSError as exc:
            write_status(root, "error", f"无法启动安装进程：{exc}", running=False, installed=False)
            return

    if proc.returncode == 0 and (target / "torch").is_dir():
        write_status(root, "done", "GPU 支持安装完成。请重启程序后选择 GPU 档。",
                     running=False, installed=True)
    else:
        write_status(root, "error",
                     "安装失败。请查看 data\\mixer\\gpu\\install.log，或检查网络/磁盘空间。",
                     running=False, installed=False)


def start_install(root: Path, preferred: str | None = None) -> dict:
    """触发后台安装；若已在安装中则直接返回当前状态。"""
    current = read_status(root)
    if current.get("running"):
        return {"ok": False, "status": current,
                "error": "正在安装中，请稍候"}

    python = find_python(root, preferred)
    if python is None:
        return {"ok": False, "status": current,
                "error": "未找到可用的 Python 3.11。请先安装 Python 3.11（python.org），再点下载。"}

    write_status(root, "installing", "准备下载…", running=True, installed=False)
    thread = threading.Thread(target=_run_install, args=(root, python), daemon=True)
    thread.start()
    return {"ok": True, "status": read_status(root), "python": python}


def is_installed(root: Path) -> bool:
    return (gpu_packages_dir(root) / "torch").is_dir()


def native_preference_path(root: Path) -> Path:
    """记录「使用本机原生 Python 环境」选择的持久化文件。"""
    return status_dir(root) / "native.json"


def read_native_preference(root: Path) -> dict | None:
    """读取用户已选择「复用本机 Python 环境」的记录。"""
    p = native_preference_path(root)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("enabled") and data.get("sitePackages"):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def set_native_preference(root: Path, enabled: bool, native: dict | None = None) -> None:
    """保存或清除「复用本机 Python 环境」的选择。"""
    p = native_preference_path(root)
    if enabled and native:
        p.write_text(json.dumps({
            "enabled": True,
            "python": native.get("python", ""),
            "sitePackages": native.get("sitePackages", ""),
            "torchVersion": native.get("torchVersion", ""),
        }, ensure_ascii=False), encoding="utf-8")
    else:
        if p.is_file():
            p.unlink()


def apply_native_to_syspath(root: Path) -> bool:
    """把用户选择的「本机原生 Python site-packages」加入 sys.path，供 import torch 命中。

    返回是否成功加入。这是「复用本机环境」的落地：不改主包、不下载，
    引擎启动时优先把本机已装好的 torch 目录接进来。
    """
    pref = read_native_preference(root)
    if not pref:
        return False
    site = str(pref.get("sitePackages") or "").strip()
    if not site or not Path(site).is_dir():
        return False
    if site not in sys.path:
        sys.path.insert(0, site)
    # torch 的 C 扩展依赖它自带的 CUDA 运行库（torch/lib 下），
    # 在 frozen 引擎里手动加入 DLL 搜索路径，确保 import 时不缺 DLL。
    try:
        lib_dir = Path(site) / "torch" / "lib"
        if lib_dir.is_dir() and hasattr(os, "add_dll_directory"):
            os.add_dll_directory(str(lib_dir))
    except OSError:
        pass
    return True

