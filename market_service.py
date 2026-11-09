"""插件市场与自动更新（无 UI 依赖）。

设计约束（对应路线图 §2 / §5）：
  * 只拉取**目录元数据**，不代用户安装来源不明的二进制；安装包必须带 SHA-256 校验。
  * 目录可指向官方发布页（type=link，按钮打开浏览器），也可指向直链 zip（type=zip，可一键安装）。
  * 安装到**当前用户**的 VST3 目录 %LOCALAPPDATA%\\Programs\\Common\\VST3，不需要管理员权限。
  * 解压做路径穿越防护，只接受 .vst3 / .dll 内容。
  * 更新检查走公开 GitHub API，失败/限频/离线都不得影响程序启动；下载校验 SHA-256 后才允许安装。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

APP_VERSION = "1.0.0"
REPO = "HandaQAQ-Code/Intro-Voice"
CATALOG_URL = f"https://raw.githubusercontent.com/{REPO}/main/plugin-registry/catalog.json"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"
MAX_DOWNLOAD_MB = 800
USER_AGENT = f"RecordOutMixer/{APP_VERSION}"


def _paths(root: Path) -> dict:
    data = root / "data" / "mixer"
    return {
        "data": data,
        "catalog": data / "market_cache.json",
        "installed": data / "market_installed.json",
        "update": data / "update",
    }


def user_vst3_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Programs" / "Common" / "VST3"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _get(url: str, timeout: float = 10.0, accept: str = "application/json") -> tuple[bytes, dict]:
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": accept,
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(), dict(response.headers)


def _download(url: str, target: Path, timeout: float = 30.0, progress=None) -> int:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    limit = MAX_DOWNLOAD_MB * 1024 * 1024
    total = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(request, timeout=timeout) as response, open(target, "wb") as fh:
        length = int(response.headers.get("Content-Length") or 0)
        while True:
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise RuntimeError(f"下载超过 {MAX_DOWNLOAD_MB} MB 上限，已中止")
            fh.write(chunk)
            if progress and length:
                progress(total, length)
    return total


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 目录
# ---------------------------------------------------------------------------
def _validate_entry(item: dict) -> dict | None:
    slug = str(item.get("slug") or "").strip()
    name = str(item.get("name") or "").strip()
    if not slug or not name:
        return None
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", slug):
        return None
    kind = str(item.get("type") or "link").lower()
    return {
        "slug": slug,
        "name": name,
        "vendor": str(item.get("vendor") or ""),
        "version": str(item.get("version") or ""),
        "license": str(item.get("license") or ""),
        "summary": str(item.get("summary") or ""),
        "homepage": str(item.get("homepage") or ""),
        "type": kind if kind in ("zip", "link") else "link",
        "download": str(item.get("download") or ""),
        "sha256": str(item.get("sha256") or "").lower(),
        "sizeMb": float(item.get("sizeMb") or 0),
        "copyright": str(item.get("copyright") or ""),
        "include": [str(x) for x in (item.get("include") or []) if str(x).strip()],
    }


def fetch_catalog(root: Path, url: str | None = None, force: bool = False) -> dict:
    paths = _paths(root)
    cached = {}
    try:
        cached = json.loads(paths["catalog"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = {}

    target = url or CATALOG_URL
    if not force and cached.get("fetchedAt") and time.time() - float(cached["fetchedAt"]) < 600:
        return {**cached, "cached": True}

    try:
        raw, headers = _get(target, timeout=8.0)
        payload = json.loads(raw.decode("utf-8"))
        entries = [e for e in (_validate_entry(i) for i in payload.get("plugins") or []) if e]
        result = {
            "source": target,
            "fetchedAt": time.time(),
            "updatedAt": str(payload.get("updatedAt") or ""),
            "plugins": entries,
            "cached": False,
        }
        try:
            paths["catalog"].parent.mkdir(parents=True, exist_ok=True)
            paths["catalog"].write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
        return result
    except Exception as exc:
        if cached.get("plugins"):
            return {**cached, "cached": True, "error": f"远程目录不可用，显示上次缓存：{exc}"}
        bundled = root / "plugin-registry" / "catalog.json"
        if bundled.is_file():
            try:
                payload = json.loads(bundled.read_text(encoding="utf-8"))
                entries = [e for e in (_validate_entry(i) for i in payload.get("plugins") or []) if e]
                if entries:
                    return {"source": str(bundled), "plugins": entries, "cached": True,
                            "bundled": True, "error": f"远程目录不可用，已加载随包目录：{exc}"}
            except (OSError, ValueError):
                pass
        return {"source": target, "plugins": [], "cached": False,
                "error": f"无法获取插件目录：{exc}"}


def installed_map(root: Path) -> dict:
    try:
        data = json.loads(_paths(root)["installed"].read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_installed(root: Path, data: dict) -> None:
    target = _paths(root)["installed"]
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".pending")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, target)


def installed_plugins(root: Path) -> list[dict]:
    result = []
    for slug, info in installed_map(root).items():
        folder = Path(info.get("path") or "")
        if folder.is_dir():
            result.append({"slug": slug, "name": info.get("name", slug),
                           "version": info.get("version", ""), "path": str(folder)})
        else:
            result.append({"slug": slug, "name": info.get("name", slug),
                           "version": info.get("version", ""), "path": str(folder), "broken": True})
    return result


# ---------------------------------------------------------------------------
# 安装 / 卸载
# ---------------------------------------------------------------------------
def _safe_extract(archive: Path, target: Path, only: list[str] | None = None) -> int:
    """解压安装包。only 非空时只保留匹配前缀的条目（例如只要 Effects 那个 bundle）。"""
    count = 0
    prefixes = [p.replace("\\", "/") for p in (only or [])]
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if prefixes and not any(name.startswith(p) for p in prefixes):
                continue
            if name.startswith("/") or ".." in name.split("/"):
                raise RuntimeError(f"压缩包包含非法路径：{info.filename}")
            if info.is_dir():
                continue
            suffix = Path(name).suffix.lower()
            if suffix not in (".vst3", ".dll", ".json", ".txt", ".md", ".license"):
                continue
            dest = (target / name).resolve()
            if not str(dest).startswith(str(target.resolve())):
                raise RuntimeError("压缩包路径越界")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            count += 1
    return count


def install(root: Path, entry: dict, progress=None) -> dict:
    if entry.get("type") != "zip" or not entry.get("download"):
        raise RuntimeError("该条目没有可直接安装的安装包，请打开官方页面")
    if not entry.get("sha256"):
        raise RuntimeError("该条目缺少 SHA-256 校验值，已拒绝安装")

    target_dir = user_vst3_dir() / entry["slug"]
    if target_dir.exists():
        raise RuntimeError(f"已安装：{target_dir}")

    paths = _paths(root)
    staging = Path(tempfile.mkdtemp(prefix="rom-", dir=str(paths["data"])))
    archive = staging / "package.zip"
    try:
        _download(entry["download"], archive, progress=progress)
        digest = _sha256(archive)
        if digest.lower() != entry["sha256"].lower():
            raise RuntimeError(f"SHA-256 不匹配（期望 {entry['sha256'][:12]}…，实际 {digest[:12]}…）")
        unpacked = staging / "unpacked"
        unpacked.mkdir()
        count = _safe_extract(archive, unpacked, entry.get("include"))
        if count == 0:
            raise RuntimeError("安装包内没有 .vst3 / .dll 文件")
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(unpacked, target_dir)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    data = installed_map(root)
    data[entry["slug"]] = {"name": entry["name"], "version": entry.get("version", ""),
                           "path": str(target_dir), "installedAt": time.strftime("%Y-%m-%d %H:%M:%S")}
    _save_installed(root, data)
    return {"installed": data[entry["slug"]], "path": str(target_dir)}


def uninstall(root: Path, slug: str) -> dict:
    data = installed_map(root)
    info = data.get(slug)
    if not info:
        raise RuntimeError("未安装该插件")
    folder = Path(info.get("path") or "")
    base = user_vst3_dir().resolve()
    try:
        resolved = folder.resolve()
    except OSError:
        resolved = folder
    if str(resolved).startswith(str(base)) and resolved.exists():
        shutil.rmtree(resolved, ignore_errors=True)
    data.pop(slug, None)
    _save_installed(root, data)
    return {"removed": slug}


# ---------------------------------------------------------------------------
# 自动更新
# ---------------------------------------------------------------------------
def _semver(text: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", str(text))
    return tuple(int(n) for n in numbers[:3]) or (0,)


def check_update(root: Path, use_proxy: str | None = None) -> dict:
    try:
        raw, _ = _get(RELEASES_API, timeout=8.0, accept="application/vnd.github+json")
        payload = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"ok": False, "error": "仓库还没有发布版本（Releases 为空）"}
        return {"ok": False, "error": f"更新服务返回 {exc.code}（可能触发限频）"}
    except Exception as exc:
        return {"ok": False, "error": f"检查更新失败：{exc}"}

    tag = str(payload.get("tag_name") or "")
    latest = tag.lstrip("vV")
    available = _semver(latest) > _semver(APP_VERSION)
    assets = []
    for item in payload.get("assets") or []:
        assets.append({"name": item.get("name"), "size": item.get("size"),
                       "url": item.get("browser_download_url")})
    return {
        "ok": True,
        "current": APP_VERSION,
        "latest": latest,
        "available": available,
        "notes": str(payload.get("body") or "")[:4000],
        "publishedAt": str(payload.get("published_at") or ""),
        "page": str(payload.get("html_url") or ""),
        "assets": assets,
    }


def download_update(root: Path, asset_url: str, expected_sha: str = "", progress=None) -> dict:
    paths = _paths(root)
    staging = paths["update"]
    staging.mkdir(parents=True, exist_ok=True)
    name = Path(asset_url).name or "update.zip"
    target = staging / name
    if target.exists():
        target.unlink()
    _download(asset_url, target, timeout=60.0, progress=progress)
    digest = _sha256(target)
    if expected_sha and digest.lower() != expected_sha.lower():
        target.unlink()
        raise RuntimeError("更新包 SHA-256 不匹配，已删除")
    (staging / "package.sha256").write_text(digest, encoding="utf-8")
    return {"file": str(target), "sha256": digest, "size": target.stat().st_size}


def write_apply_script(root: Path, package: Path) -> Path:
    """生成等待主程序退出后替换文件、失败回滚的脚本（不自动提权）。"""
    paths = _paths(root)
    script = paths["update"] / "apply_update.bat"
    app_dir = root / "RecordOutMixer"
    backup = root / "RecordOutMixer.bak"
    body = f"""@echo off
setlocal
rem Record Out 自动更新：等待主程序退出 -> 备份 -> 解压覆盖 -> 失败回滚
cd /d "{root}"
:wait
tasklist /FI "IMAGENAME eq RecordOutMixer.exe" 2>nul | find /I "RecordOutMixer.exe" >nul
if not errorlevel 1 (
  timeout /t 1 /nobreak >nul
  goto wait
)
if exist "{backup}" rmdir /s /q "{backup}"
if exist "{app_dir}" move "{app_dir}" "{backup}" >nul
powershell -NoProfile -Command "Expand-Archive -LiteralPath '{package}' -DestinationPath '{app_dir}' -Force"
if errorlevel 1 goto rollback
start "" "{app_dir}\\RecordOutMixer.exe"
timeout /t 5 /nobreak >nul
if exist "{backup}" rmdir /s /q "{backup}"
exit /b 0
:rollback
if exist "{app_dir}" rmdir /s /q "{app_dir}"
if exist "{backup}" move "{backup}" "{app_dir}" >nul
echo 更新失败，已回滚到上一版本。请查看 data\\mixer\\update.log
pause
exit /b 1
"""
    script.write_text(body.replace("\n", "\r\n"), encoding="gbk", errors="replace")
    return script
