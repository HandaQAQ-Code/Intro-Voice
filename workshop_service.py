"""创意工坊：用户自己写的声音插件（Python 效果脚本）。

插件格式
--------
``data/mixer/workshop/<slug>/plugin.json``

```json
{
  "schema": 1,
  "slug": "air-detail",
  "name": "空气感",
  "author": "你的名字",
  "version": "1.0.0",
  "license": "MIT",
  "description": "一句话说明",
  "code": "PARAMS = [...]\ndef process(x, sr, params): ..."
}
```

脚本约定
--------
* 顶层可定义 ``PARAMS``（列表），引擎会解析成界面滑块。
* 必须定义 ``process(x, sr, params)``：
  - ``x``：``float32``，形状 ``(块长度, 2)``，左右声道；
  - ``sr``：采样率（当前链路固定 48000）；
  - ``params``：``dict[str, float]``，按 ``PARAMS`` 归一化后的值。
  - 返回值必须是同形状数组；越界/NaN 会被旁通并计数。
* 可用 ``np`` / ``numpy``，以及 ``db_to_gain`` / ``gain_to_db`` / ``soft_clip`` 三个便捷函数。
* 代码在**音频进程**内以本机权限执行，只应运行你信任的脚本。

设计约束
--------
* 控制线程负责编译与参数更新，音频线程只做非阻塞取快照，绝不在回调里 exec。
* 单个插件异常只旁通该插件，不影响整条链路。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np

WORKSHOP_VERSION = 1
# 市场仓库以 Gitee 为国内主源（发布也推到这里）。
GITEE_OWNER = "handa2500"
MARKET_REPO = f"{GITEE_OWNER}/intro-voice-plugin-market"
MARKET_WEB = f"https://gitee.com/{MARKET_REPO}"
MARKET_SSH = f"git@gitee.com:{MARKET_REPO}.git"
MARKET_RAW = f"{MARKET_WEB}/raw/main"
MAX_CODE_BYTES = 96 * 1024
MAX_PLUGINS = 32
TEST_RATE = 48000
TEST_BLOCK = 1024


# ---------------------------------------------------------------------------
# 路径与校验
# ---------------------------------------------------------------------------
def workshop_dir(root: Path) -> Path:
    return root / "data" / "mixer" / "workshop"


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug[:48] or "plugin"


def _number(value, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _param_defs(raw) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("id") or "").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,31}", pid) or pid in seen:
            continue
        seen.add(pid)
        lo = _number(item.get("min"), 0.0)
        hi = _number(item.get("max"), 1.0)
        if hi <= lo:
            hi = lo + 1.0
        default = min(max(_number(item.get("default"), lo), lo), hi)
        result.append({
            "id": pid,
            "name": str(item.get("name") or pid)[:40],
            "min": lo,
            "max": hi,
            "default": default,
            "step": max(0.0, _number(item.get("step"), 0.0)),
            "unit": str(item.get("unit") or "")[:8],
        })
    return result


def normalize_params(defs: list[dict], values: dict | None) -> dict:
    out = {}
    values = values if isinstance(values, dict) else {}
    for item in defs:
        value = _number(values.get(item["id"]), item["default"])
        out[item["id"]] = min(max(value, item["min"]), item["max"])
    return out


def _validate(manifest: dict) -> dict:
    if not isinstance(manifest, dict):
        raise RuntimeError("插件描述不是对象")
    name = str(manifest.get("name") or "").strip()
    if not name:
        raise RuntimeError("插件名称不能为空")
    slug = slugify(str(manifest.get("slug") or name))
    code = str(manifest.get("code") or "")
    if not code.strip():
        raise RuntimeError("效果代码不能为空")
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise RuntimeError("效果代码超过 96 KB 上限")
    license_name = str(manifest.get("license") or "MIT").strip() or "MIT"
    return {
        "schema": WORKSHOP_VERSION,
        "slug": slug,
        "name": name[:60],
        "author": str(manifest.get("author") or "匿名")[:40],
        "version": str(manifest.get("version") or "1.0.0")[:20],
        "license": license_name[:20],
        "description": str(manifest.get("description") or "")[:400],
        "code": code,
        "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# 编译与执行
# ---------------------------------------------------------------------------
_SAFE_BUILTINS = {
    "abs": abs, "min": min, "max": max, "range": range, "len": len,
    "enumerate": enumerate, "zip": zip, "sum": sum, "pow": pow, "round": round,
    "float": float, "int": int, "list": list, "dict": dict, "tuple": tuple,
    "sorted": sorted, "any": any, "all": all, "map": map, "filter": filter,
    "ValueError": ValueError, "Exception": Exception,
}


def _helpers() -> dict:
    def db_to_gain(db):
        return 10.0 ** (np.asarray(db, dtype=np.float32) / 20.0)

    def gain_to_db(gain):
        return 20.0 * np.log10(np.maximum(np.asarray(gain, dtype=np.float32), 1e-9))

    def soft_clip(x, drive=1.0):
        drive = float(np.clip(drive, 1.0, 24.0))
        return np.tanh(np.asarray(x, dtype=np.float32) * drive) / np.tanh(drive)

    return {"db_to_gain": db_to_gain, "gain_to_db": gain_to_db, "soft_clip": soft_clip}


class Compiled:
    """一次编译的结果：函数、参数定义、错误信息。"""

    def __init__(self, slug: str, manifest: dict | None = None, error: str = ""):
        self.slug = slug
        self.manifest = manifest or {}
        self.fn = None
        self.params: list[dict] = []
        self.error = error


def compile_manifest(manifest: dict) -> Compiled:
    slug = str(manifest.get("slug") or "plugin")
    try:
        checked = _validate(manifest)
        namespace = {
            "__builtins__": dict(_SAFE_BUILTINS),
            "np": np,
            "numpy": np,
            **_helpers(),
        }
        exec(compile(check(checked["code"], slug), f"<workshop:{slug}>", "exec"), namespace)
        fn = namespace.get("process")
        if not callable(fn):
            raise RuntimeError("脚本里没有定义 process(x, sr, params)")
        compiled = Compiled(slug, checked)
        compiled.fn = fn
        compiled.params = _param_defs(namespace.get("PARAMS"))
        return compiled
    except Exception as exc:  # noqa: BLE001 - 用户代码，任何异常都要落到界面
        message = f"{type(exc).__name__}: {exc}"
        return Compiled(slug, manifest, message)


def _source_check(code: str) -> str:
    """限制明显的文件/网络访问，让工坊插件只做音频处理。"""
    if re.search(r"(?m)^\s*(import|from)\s+", code):
        raise RuntimeError("效果代码不能 import 模块；请使用内置的 np / db_to_gain / gain_to_db / soft_clip")
    for token in ("__import__", "eval(", "exec(", "open(", "subprocess", "socket", "urllib"):
        if token in code:
            raise RuntimeError(f"效果代码不能使用 {token}")
    return code


def check(code: str, slug: str) -> str:
    return _source_check(code)


# ---------------------------------------------------------------------------
# 插件文件
# ---------------------------------------------------------------------------
def plugin_dir(root: Path, slug: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", slug):
        raise RuntimeError("插件标识无效")
    return workshop_dir(root) / slug


def load_manifest(root: Path, slug: str) -> dict:
    path = plugin_dir(root, slug) / "plugin.json"
    if not path.is_file():
        raise RuntimeError("工坊插件不存在")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("插件文件格式错误")
    return payload


def list_plugins(root: Path) -> list[dict]:
    base = workshop_dir(root)
    result: list[dict] = []
    if not base.is_dir():
        return result
    for path in sorted(base.glob("*/plugin.json"), key=lambda p: p.parent.name):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        result.append({
            "schema": payload.get("schema", WORKSHOP_VERSION),
            "slug": path.parent.name,
            "name": str(payload.get("name") or path.parent.name),
            "author": str(payload.get("author") or ""),
            "version": str(payload.get("version") or ""),
            "license": str(payload.get("license") or ""),
            "description": str(payload.get("description") or ""),
            "updatedAt": str(payload.get("updatedAt") or ""),
            "code": str(payload.get("code") or ""),
            "sizeKb": round(path.stat().st_size / 1024, 1),
        })
    return result


def save_plugin(root: Path, manifest: dict) -> dict:
    checked = _validate(manifest)
    compiled = compile_manifest(checked)
    if compiled.fn is None:
        raise RuntimeError(f"代码编译失败：{compiled.error}")
    target = plugin_dir(root, checked["slug"])
    if not target.exists() and len(list_plugins(root)) >= MAX_PLUGINS:
        raise RuntimeError(f"本机工坊插件已达 {MAX_PLUGINS} 个上限")
    target.mkdir(parents=True, exist_ok=True)
    temp = target / "plugin.pending"
    temp.write_text(json.dumps(checked, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, target / "plugin.json")
    return {**checked, "params": compiled.params, "slug": checked["slug"]}


def delete_plugin(root: Path, slug: str) -> dict:
    target = plugin_dir(root, slug)
    if not target.is_dir():
        raise RuntimeError("工坊插件不存在")
    shutil.rmtree(target, ignore_errors=True)
    return {"removed": slug}


def export_zip(root: Path, slug: str) -> Path:
    manifest = load_manifest(root, slug)
    out_dir = root / "data" / "mixer" / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{slug}-{manifest.get('version', '1.0.0')}.zip"
    import zipfile
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{slug}/plugin.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr(f"{slug}/README.md",
                    f"# {manifest.get('name')}\n\n{manifest.get('description', '')}\n\n"
                    f"- 作者：{manifest.get('author', '')}\n"
                    f"- 许可：{manifest.get('license', '')}\n"
                    f"- 版本：{manifest.get('version', '')}\n")
    return target


# ---------------------------------------------------------------------------
# 试听 / 指标
# ---------------------------------------------------------------------------
def test_manifest(manifest: dict) -> dict:
    compiled = compile_manifest(manifest)
    if compiled.fn is None:
        return {"ok": False, "error": compiled.error, "params": []}

    rng = np.random.default_rng(7)
    total = TEST_RATE
    t = np.arange(total) / TEST_RATE
    mono = 0.12 * np.sin(2 * np.pi * 440.0 * t) + 0.02 * rng.standard_normal(total)
    mono = np.clip(mono, -0.98, 0.98).astype(np.float32)
    signal = np.stack([mono, mono * 0.85], axis=1)
    params = normalize_params(compiled.params, {})

    out = np.zeros_like(signal)
    elapsed = 0.0
    try:
        for start in range(0, total, TEST_BLOCK):
            block = signal[start:start + TEST_BLOCK]
            began = time.perf_counter()
            piece = compiled.fn(block.copy(), TEST_RATE, dict(params))
            elapsed += time.perf_counter() - began
            piece = np.asarray(piece, dtype=np.float32)
            if piece.shape != block.shape:
                raise RuntimeError(f"process 返回形状 {piece.shape}，应为 {block.shape}")
            if not np.isfinite(piece).all():
                raise RuntimeError("process 产生了 NaN 或无穷大")
            out[start:start + len(block)] = piece
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "params": compiled.params}

    blocks = max(1, total // TEST_BLOCK)

    def rms(x):
        return float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))

    before, after = rms(signal), rms(out)
    return {
        "ok": True,
        "error": "",
        "params": compiled.params,
        "rmsIn": round(before, 5),
        "rmsOut": round(after, 5),
        "changeDb": round(20 * np.log10(max(after, 1e-9) / max(before, 1e-9)), 2),
        "peak": round(float(np.max(np.abs(out))), 4),
        "msPerBlock": round(elapsed * 1000 / blocks, 3),
    }


# ---------------------------------------------------------------------------
# 音频线程执行器
# ---------------------------------------------------------------------------
class Stage:
    __slots__ = ("slug", "name", "fn", "params", "enabled")

    def __init__(self, slug: str, name: str, fn, params: dict, enabled: bool = True):
        self.slug = slug
        self.name = name
        self.fn = fn
        self.params = params
        self.enabled = enabled


class WorkshopRunner:
    """按顺序执行工坊插件。控制线程换快照，音频线程非阻塞读取。"""

    def __init__(self, rate: int = 48000):
        self.rate = rate
        self._stages: list[Stage] = []
        self._pending: list[Stage] | None = None
        self._pending_params: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.errors = 0
        self.last_error = ""

    def set_stages(self, stages: list[Stage]) -> None:
        with self._lock:
            self._pending = stages

    def update_params(self, slug: str, params: dict) -> None:
        """滑块拖动时只改数值，不重新编译脚本。"""
        for stage in self._stages:
            if stage.slug == slug:
                stage.params.update(params)
        self._pending_params.setdefault(slug, {}).update(params)

    def _apply(self) -> None:
        pending = self._pending
        if pending is not None:
            self._pending = None
            self._stages = pending
        if self._pending_params:
            for stage in self._stages:
                if stage.slug in self._pending_params:
                    stage.params.update(self._pending_params[stage.slug])
            self._pending_params.clear()

    def process(self, x: np.ndarray) -> np.ndarray:
        if self._lock.acquire(blocking=False):
            try:
                self._apply()
            finally:
                self._lock.release()
        for stage in self._stages:
            if not stage.enabled:
                continue
            try:
                y = stage.fn(x.copy(), self.rate, dict(stage.params))
                y = np.asarray(y, dtype=np.float32)
                if y.shape != x.shape or not np.isfinite(y).all():
                    raise RuntimeError("输出形状不符或包含非有限值")
                x = y
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                self.last_error = f"{stage.name}: {type(exc).__name__}: {exc}"
        return x

    def metrics(self) -> dict:
        return {"count": len(self._stages), "errors": self.errors, "lastError": self.last_error}


def build_stages(root: Path, state: dict | None) -> tuple[list[Stage], list[str]]:
    """按 settings.workshop 构建立即可以跑的阶段列表。"""
    state = state if isinstance(state, dict) else {}
    order = [s for s in (state.get("order") or []) if isinstance(s, str)]
    enabled_map = state.get("enabled") or {}
    params_map = state.get("params") or {}
    stages: list[Stage] = []
    errors: list[str] = []
    for slug in order[:MAX_PLUGINS]:
        try:
            manifest = load_manifest(root, slug)
            compiled = compile_manifest(manifest)
            if compiled.fn is None:
                errors.append(f"{manifest.get('name', slug)}：{compiled.error}")
                continue
            stages.append(Stage(
                slug=slug,
                name=str(manifest.get("name") or slug),
                fn=compiled.fn,
                params=normalize_params(compiled.params, params_map.get(slug) or {}),
                enabled=bool(enabled_map.get(slug, True)),
            ))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{slug}：{exc}")
    return stages, errors


# ---------------------------------------------------------------------------
# 市场安装与发布
# ---------------------------------------------------------------------------
def install_from_market(root: Path, entry: dict) -> dict:
    url = str(entry.get("manifest") or "")
    if not url.startswith("http"):
        raise RuntimeError("市场条目缺少插件清单地址")
    request = urllib.request.Request(url, headers={"User-Agent": "IntroVoice/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("插件清单格式错误")
    expected = str(entry.get("slug") or "")
    if expected and slugify(str(payload.get("slug") or "")) != expected:
        raise RuntimeError("插件清单与目录条目不匹配")
    saved = save_plugin(root, payload)
    target = plugin_dir(root, saved["slug"])
    return {"slug": saved["slug"], "path": str(target), "name": saved["name"]}


def _git(args: list[str], cwd: Path) -> str:
    env = dict(os.environ)
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o StrictHostKeyChecking=no")
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                            timeout=180, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else f"git {args[0]} 失败")
    return result.stdout.strip()


def _market_entry(manifest: dict) -> dict:
    return {
        "slug": manifest["slug"],
        "name": manifest["name"],
        "vendor": manifest.get("author") or "",
        "version": manifest.get("version") or "1.0.0",
        "license": manifest.get("license") or "MIT",
        "copyright": f"© {manifest.get('author') or '匿名'} · {manifest.get('license') or 'MIT'}",
        "type": "workshop",
        "summary": manifest.get("description") or "",
        "homepage": f"{MARKET_WEB}/tree/main/plugins/{manifest['slug']}",
        "manifest": f"{MARKET_RAW}/plugins/{manifest['slug']}/plugin.json",
        "sizeMb": round(len(json.dumps(manifest, ensure_ascii=False)) / 1024, 1),
    }


def publish(root: Path, slug: str, message: str = "") -> dict:
    """把工坊插件推到自托管的插件市场仓库，并重建 catalog.json。"""
    root = Path(root).resolve()
    manifest = load_manifest(root, slug)
    repo = root / "data" / "mixer" / "market_repo"
    if not (repo / ".git").is_dir():
        shutil.rmtree(repo, ignore_errors=True)
        repo.parent.mkdir(parents=True, exist_ok=True)
        _git(["clone", "--depth", "1", MARKET_SSH, str(repo.resolve())], cwd=root)
        if not (repo / ".git").is_dir():
            raise RuntimeError("市场仓库克隆失败，请检查网络与 SSH 权限")
        top = Path(_git(["rev-parse", "--show-toplevel"], cwd=repo)).resolve()
        origin = _git(["remote", "get-url", "origin"], cwd=repo)
        if top != repo.resolve() or "plugin-market" not in origin.lower():
            shutil.rmtree(repo, ignore_errors=True)
            raise RuntimeError(f"市场仓库定位异常（{origin} → {top}），已中止，未做任何提交")
    else:
        try:
            _git(["pull", "--ff-only"], cwd=repo)
        except RuntimeError:
            _git(["fetch", "origin", "main"], cwd=repo)
            _git(["reset", "--hard", "origin/main"], cwd=repo)

    plugins_dir = repo / "plugins" / manifest["slug"]
    plugins_dir.mkdir(parents=True, exist_ok=True)
    (plugins_dir / "plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (plugins_dir / "README.md").write_text(
        f"# {manifest['name']}\n\n{manifest.get('description', '')}\n\n"
        f"| 项 | 值 |\n| --- | --- |\n"
        f"| 作者 | {manifest.get('author', '')} |\n"
        f"| 版本 | {manifest.get('version', '')} |\n"
        f"| 许可 | {manifest.get('license', '')} |\n"
        f"| 标识 | `{manifest['slug']}` |\n\n"
        f"在 Intro Voice 音拓 → 插件市场 → 刷新目录即可一键安装。\n",
        encoding="utf-8")

    catalog_path = repo / "catalog.json"
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        catalog = {"schema": 1, "plugins": []}
    entries = [e for e in catalog.get("plugins") or [] if e.get("slug") != manifest["slug"]]
    entries.insert(0, _market_entry(manifest))
    catalog["schema"] = 1
    catalog["updatedAt"] = time.strftime("%Y-%m-%d")
    catalog["plugins"] = entries
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")

    _git(["add", "-A"], cwd=repo)
    status = _git(["status", "--porcelain"], cwd=repo)
    if status:
        text = message.strip() or f"plugin: add {manifest['slug']} {manifest.get('version', '')}"
        _git(["-c", "user.name=" + (manifest.get("author") or "Intro Voice"),
              "-c", "user.email=plugins@introvoice.local",
              "commit", "-m", text], cwd=repo)
        _git(["push", "origin", "HEAD:main"], cwd=repo)
    commit = _git(["rev-parse", "--short", "HEAD"], cwd=repo)

    # 本地随包目录同步一份，断网时仍能在市场里看到自己发布的插件。
    bundled = root / "plugin-market"
    try:
        (bundled / "plugins" / manifest["slug"]).mkdir(parents=True, exist_ok=True)
        shutil.copy2(plugins_dir / "plugin.json", bundled / "plugins" / manifest["slug"] / "plugin.json")
        shutil.copy2(plugins_dir / "README.md", bundled / "plugins" / manifest["slug"] / "README.md")
        bundled.mkdir(parents=True, exist_ok=True)
        (bundled / "catalog.json").write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass

    return {
        "slug": manifest["slug"],
        "commit": commit,
        "changed": bool(status),
        "repo": MARKET_REPO,
        "url": f"{MARKET_WEB}/tree/main/plugins/{manifest['slug']}",
        "catalog": f"{MARKET_RAW}/catalog.json",
    }
