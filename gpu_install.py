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
import re
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


def _py_launcher_pythons() -> list[str]:
    """用 py launcher 列出所有已安装的 Python 解释器（最全的来源）。"""
    out_paths: list[str] = []
    try:
        out = subprocess.run(["py", "-0p"], **_run_kwargs(timeout=10))
    except (OSError, subprocess.SubprocessError):
        return out_paths
    for line in (out.stdout or "").splitlines():
        # 形如： -V:3.11 *        C://...//Python311//python.exe
        m = re.search(r"([A-Za-z]:\\.*?python(?:w)?\.exe)\s*$", line.strip(), re.IGNORECASE)
        if m:
            out_paths.append(m.group(1).strip())
    return out_paths


def _where_pythons() -> list[str]:
    """PATH 里能直接找到的 python（where python / python3）。"""
    out_paths: list[str] = []
    for name in ("python", "python3"):
        try:
            out = subprocess.run(["where", name], **_run_kwargs(timeout=8))
        except (OSError, subprocess.SubprocessError):
            continue
        for line in (out.stdout or "").splitlines():
            path = line.strip()
            if path.lower().endswith("python.exe") and Path(path).is_file():
                out_paths.append(path)
    return out_paths


def _conda_env_pythons() -> list[str]:
    """Conda / MiniConda 下所有环境的 python.exe（用户经常把 CUDA 环境建在这里）。"""
    out_paths: list[str] = []
    bases = [
        Path(os.environ.get("USERPROFILE", "")) / "anaconda3",
        Path(os.environ.get("USERPROFILE", "")) / "miniconda3",
        Path(os.environ.get("LOCALAPPDATA", "")) / "anaconda3",
        Path(os.environ.get("LOCALAPPDATA", "")) / "miniconda3",
        Path(os.environ.get("PROGRAMDATA", "")) / "anaconda3",
        Path(os.environ.get("PROGRAMDATA", "")) / "miniconda3",
        Path("C:/ProgramData/miniconda3"),
        Path("C:/ProgramData/anaconda3"),
    ]
    for base in bases:
        exe = base / "python.exe"
        if exe.is_file():
            out_paths.append(str(exe))
        envs = base / "envs"
        if not envs.is_dir():
            continue
        try:
            for entry in envs.iterdir():
                exe = entry / "python.exe"
                if entry.is_dir() and exe.is_file():
                    out_paths.append(str(exe))
        except OSError:
            continue
    return out_paths


def _common_dir_pythons() -> list[str]:
    """扫描常见安装目录里所有 Python3XX。"""
    out_paths: list[str] = []
    local_apps = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python"
    if local_apps.is_dir():
        try:
            for entry in local_apps.iterdir():
                if entry.is_dir():
                    exe = entry / "python.exe"
                    if exe.is_file():
                        out_paths.append(str(exe))
        except OSError:
            pass

    roots: list[Path] = [Path("C:/")]
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
        base = os.environ.get(env)
        if base:
            roots.append(Path(base))
    for root in roots:
        try:
            for entry in root.iterdir():
                if not entry.is_dir():
                    continue
                name = entry.name.lower().replace(" ", "")
                if name.startswith("python3"):
                    exe = entry / "python.exe"
                    if exe.is_file():
                        out_paths.append(str(exe))
        except OSError:
            continue
    return out_paths


def discover_pythons(root: Path, preferred: str | None = None) -> list[dict]:
    """自动搜索本机所有 Python 解释器，返回 [{python, version, major, minor, compatible}]。

    来源：用户指定路径 → 内置 runtime → py launcher 全部版本 → PATH →
    %LOCALAPPDATA%\\Programs\\Python → C://// 与 Program Files 下的 Python3XX。
    这是「处理多 Python 情况」的基础：把所有候选都列出来再逐个探测。
    """
    candidates: list[str] = []
    if preferred and Path(preferred).is_file():
        candidates.append(preferred)
    for name in ("python.exe", "pythonw.exe"):
        bundled = root / "runtime" / name
        if bundled.is_file():
            candidates.append(str(bundled))
    candidates += _py_launcher_pythons()
    candidates += _where_pythons()
    candidates += _common_dir_pythons()
    candidates += _conda_env_pythons()

    seen: set[str] = set()
    result: list[dict] = []
    for cand in candidates:
        key = cand.lower()
        if key in seen:
            continue
        seen.add(key)
        if not Path(cand).is_file():
            continue
        try:
            out = subprocess.run(
                [cand, "-c",
                 "import sys;print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
                **_run_kwargs(timeout=12))
        except (OSError, subprocess.SubprocessError):
            continue
        ver = (out.stdout or "").strip()
        if "." not in ver:
            continue
        try:
            major, minor = (int(x) for x in ver.split(".")[:2])
        except ValueError:
            continue
        result.append({"python": cand, "version": ver,
                       "major": major, "minor": minor,
                       "compatible": major == REQUIRED_PY_MAJOR and minor == REQUIRED_PY_MINOR})
    return result


def _native_candidates(root: Path, preferred: str | None = None) -> list[str]:
    """所有候选解释器路径（按优先级：指定 → 内置 → py launcher → PATH → 常见目录）。"""
    try:
        found = discover_pythons(root, preferred)
        paths = [item["python"] for item in found]
    except Exception:
        paths = []
    if preferred and Path(preferred).is_file() and preferred not in paths:
        paths.insert(0, preferred)
    if not paths:
        paths = ["py", "python", "python3"]
    return paths


_probe_cache: dict[str, tuple[float, dict | None]] = {}
PROBE_TTL_SECONDS = 300.0


def _probe_torch(python: str) -> dict | None:
    """探测某个解释器是否有可用的 CUDA torch；返回 {torchVersion, sitePackages} 或 None。

    探测要起子进程 + import torch（较慢），带 5 分钟缓存避免反复扫。
    """
    key = python.lower()
    now = time.time()
    hit = _probe_cache.get(key)
    if hit and now - hit[0] < PROBE_TTL_SECONDS:
        return hit[1]
    result = _probe_torch_uncached(python)
    _probe_cache[key] = (now, result)
    return result


def _probe_torch_uncached(python: str) -> dict | None:
    probe = (
        "import torch, os;"
        "print(torch.__version__);"
        "print('1' if torch.cuda.is_available() else '0');"
        "print(os.path.dirname(os.path.dirname(torch.__file__)))"
    )
    try:
        # 首次 import torch 要加载几 GB 的 CUDA 运行库，冷启动可能超过 30 s。
        out = subprocess.run([python, "-c", probe], **_run_kwargs(timeout=90))
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [ln.strip() for ln in (out.stdout or "").splitlines() if ln.strip()]
    if len(lines) < 3:
        return None
    version, cuda_flag, site = lines[0], lines[1], lines[2]
    if cuda_flag != "1":
        return None
    return {"python": python, "sitePackages": site,
            "torchVersion": version, "cuda": True}


def detect_native_torch(root: Path, preferred: str | None = None) -> dict | None:
    """自动搜索本机所有 Python，找出第一个已装好可用 CUDA torch 的环境。

    优先用户手动指定的路径（preferred），然后按 py launcher / PATH / 常见目录
    的顺序逐个探测——这样即使机器上装了多个 Python、只有其中一个装好了
    CUDA torch，也能被自动找到并直接复用，不需要再下载。
    """
    for cand in _native_candidates(root, preferred):
        if _python_matches(cand) is None:
            continue
        hit = _probe_torch(cand)
        if hit:
            return hit
    return None


def detect_all_native(root: Path, preferred: str | None = None) -> list[dict]:
    """列出所有 Python 及其 torch/CUDA 状态，供设置页展示（多 Python 场景）。

    每个元素：{python, version, compatible, torchVersion?, cuda?, sitePackages?}
    """
    result: list[dict] = []
    for item in discover_pythons(root, preferred):
        entry = dict(item)
        if item.get("compatible"):
            hit = _probe_torch(str(item["python"]))
            if hit:
                entry.update({"torchVersion": hit["torchVersion"],
                              "cuda": True,
                              "sitePackages": hit["sitePackages"]})
            else:
                entry.update({"cuda": False})
        else:
            entry.update({"cuda": False, "incompatible": True})
        result.append(entry)
    return result


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


# 探测缓存：发现 Python 与 torch 探测都要启动子进程，较慢；
# 用短 TTL 缓存避免每次状态轮询都重扫一遍。
_scan_cache: dict[str, tuple[float, list[dict]]] = {}
_scan_lock = threading.Lock()
SCAN_TTL_SECONDS = 300.0


def reset_caches() -> None:
    """清掉 Python 发现与 torch 探测缓存（用户换了环境/刚装完 torch 时用）。"""
    with _scan_lock:
        _scan_cache.clear()
    _probe_cache.clear()


def discover_pythons_cached(root: Path, preferred: str | None = None,
                            force: bool = False) -> list[dict]:
    """带缓存的 Python 列表（5 分钟内复用结果，避免反复起子进程）。"""
    key = str(root.resolve())
    now = time.time()
    with _scan_lock:
        hit = _scan_cache.get(key)
        if hit and not force and now - hit[0] < SCAN_TTL_SECONDS:
            return hit[1]
    try:
        found = discover_pythons(root, preferred)
    except Exception:
        found = []
    with _scan_lock:
        _scan_cache[key] = (now, found)
    return found


def auto_enable_native(root: Path, preferred: str | None = None) -> dict | None:
    """自动复用本机已装好的 CUDA torch —— 检测到就直接用，不再让用户下载。

    逻辑：
    1. 已有保存的 native.json 且目录仍有效 → 直接返回它；
    2. 否则自动搜索所有 Python，找到第一个带可用 CUDA torch 的 → 立刻写入
       native.json 并加入 sys.path，返回该环境；
    3. 都没有 → 返回 None（此时才需要下载）。

    返回 {python, sitePackages, torchVersion, cuda, reused} 或 None。
    """
    pref = read_native_preference(root)
    if pref:
        # 用户在引导/设置里换了 Python：旧记录立即作废，按新解释器重新检测。
        chosen = str(pref.get("python") or "")
        if preferred and Path(preferred).is_file() and chosen.lower() != preferred.lower():
            pref = None
    if pref:
        site = str(pref.get("sitePackages") or "")
        if site and Path(site).is_dir():
            apply_native_to_syspath(root)
            return {"python": pref.get("python", ""), "sitePackages": site,
                    "torchVersion": pref.get("torchVersion", ""),
                    "cuda": True, "reused": True}

    hit = detect_native_torch(root, preferred)
    if not hit:
        return None
    set_native_preference(root, True, hit)
    apply_native_to_syspath(root)
    hit["reused"] = True
    return hit


def scan_native_report(root: Path, preferred: str | None = None,
                       force: bool = False) -> dict:
    """给设置页用：列出本机所有 Python + 是否有可用 CUDA torch + 当前选用哪一个。

    force=True 时清缓存重新扫描（「重新扫描」按钮用）。
    """
    if force:
        _probe_cache.clear()
    pythons = discover_pythons_cached(root, preferred, force=force)
    usable = None
    entries: list[dict] = []
    for item in pythons:
        entry = dict(item)
        if item.get("compatible"):
            hit = _probe_torch(str(item["python"]))
            if hit:
                entry.update({"torchVersion": hit["torchVersion"], "cuda": True,
                              "sitePackages": hit["sitePackages"]})
                if usable is None:
                    usable = hit
            else:
                entry["cuda"] = False
        else:
            entry["cuda"] = False
            entry["incompatible"] = True
        entries.append(entry)
    pref = read_native_preference(root)
    return {
        "pythons": entries,
        "usable": usable,
        "active": pref,
        "count": len(entries),
    }


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
    # frozen 引擎只打包了自身用到的标准库模块，torch 运行期还会 import
    # pickletools、unittest.mock 等；把本机 Python 的 Lib 目录挂到末尾兜底，
    # 缺失的模块按需从磁盘加载（版本与冻结解释器一致）。
    stdlib = Path(site).parent
    if (stdlib / "os.py").is_file() and str(stdlib) not in sys.path:
        sys.path.append(str(stdlib))
    # torch 的 C 扩展依赖它自带的 CUDA 运行库（torch/lib 下），
    # 在 frozen 引擎里手动加入 DLL 搜索路径，确保 import 时不缺 DLL。
    try:
        lib_dir = Path(site) / "torch" / "lib"
        if lib_dir.is_dir():
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(str(lib_dir))
            # 部分依赖（c10.dll、nvrtc）仍按 PATH 查找；补在最前面，只增不减。
            current = os.environ.get("PATH", "")
            if str(lib_dir) not in current.split(os.pathsep):
                os.environ["PATH"] = str(lib_dir) + os.pathsep + current
    except OSError:
        pass
    return True

