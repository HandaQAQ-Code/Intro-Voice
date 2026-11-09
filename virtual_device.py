"""系统级虚拟麦克风（音频线）的检测与官方安装引导。

设计边界（与 PRODUCT_ROADMAP_MIXER_ECOSYSTEM.md / THIRD_PARTY.md 一致）：

* 本模块只**检测** Windows 音频端点、展示路由状态，并把用户引导到官方安装包或官网；
* 不分发、不复制、不自动运行 VB-CABLE 安装器，也不改动系统默认设备；
* 系统级虚拟设备是内核驱动，安装需要管理员同意，通常还需要重启，
  用户态 Python 无法凭空创建端点。

VB-CABLE 为 Vincent Burel (VB-Audio) 的捐赠软件，版权归原作者所有；
许可允许原样复制扩散，但不允许未经作者同意集成进其他软件的安装流程。
本仓库与发行包均不包含驱动文件。
"""
from __future__ import annotations

import subprocess
import webbrowser
from typing import Any

import sounddevice as sd

from app_paths import ROOT

VENDOR_URL = "https://vb-cable.com/"
DOWNLOAD_URL = "https://vb-cable.com/"
DRIVER_NOTE = ("VB-CABLE 由 VB-Audio / Vincent Burel 开发，版权归原作者所有。"
               "本软件不随包分发、也不自动安装该驱动；安装与卸载由你手动完成。")

# 仅用于「本机恰好已经解压了官方驱动包」时把它指给用户，不复制、不打包。
_LOCAL_PACK_CANDIDATES = (
    ROOT / "data" / "drivers" / "vbcable-pack46",
    ROOT / "drivers" / "vbcable-pack46",
)


def is_cable_endpoint(name: str) -> bool:
    """VB-CABLE 的 CABLE Input（播放）/ CABLE Output（录音）端点。"""
    text = (name or "").lower()
    if "16 ch" in text:  # 多通道变体不是我们默认写入的那条线
        return False
    return ("cable input" in text or "cable output" in text
            or "vb-audio virtual cable" in text or "cable in " in text)


def find_local_pack() -> dict[str, Any]:
    """本机是否已存在官方解压包；只返回路径，不读取或复制驱动内容。"""
    for folder in _LOCAL_PACK_CANDIDATES:
        if not folder.is_dir():
            continue
        setups = [folder / "VBCABLE_Setup_x64.exe", folder / "VBCABLE_Setup.exe"]
        setup = next((p for p in setups if p.is_file()), None)
        if setup is None:
            continue
        return {"present": True, "dir": str(folder), "setup": str(setup),
                "readme": str(folder / "readme.txt") if (folder / "readme.txt").is_file() else ""}
    return {"present": False, "dir": "", "setup": "", "readme": ""}


def _endpoint(info: dict, host: str, index: int) -> dict[str, Any]:
    return {
        "index": index,
        "label": f"{index}: {info['name']} [{host}]",
        "name": info["name"],
        "host": host,
        "rate": int(info.get("default_samplerate") or 0),
        "channels": int(info.get("max_output_channels") or info.get("max_input_channels") or 0),
    }


def detect(devices: list[dict] | None = None,
           hosts: list[dict] | None = None) -> dict[str, Any]:
    """枚举当前 WASAPI/MME/DirectSound 端点，判断是否已有虚拟音频线。

    调用方已经枚举过设备时可以传进来复用，避免一次命令里重复查询 PortAudio。
    """
    try:
        hosts = hosts if hosts is not None else sd.query_hostapis()
        devices = list(devices) if devices is not None else list(sd.query_devices())
    except Exception as exc:  # PortAudio 未就绪时也不能拖垮服务
        return {"installed": False, "render": None, "capture": None,
                "candidates": [], "pack": find_local_pack(),
                "vendorUrl": VENDOR_URL, "downloadUrl": DOWNLOAD_URL,
                "driverNote": DRIVER_NOTE, "error": str(exc),
                "hint": "音频设备枚举失败：" + str(exc)}
    render = None   # 本软件写入 CABLE Input（播放端点）
    capture = None  # OBS / 会议软件采集 CABLE Output（录音端点）
    candidates: list[dict[str, Any]] = []
    for index, info in enumerate(devices):
        if not is_cable_endpoint(info["name"]):
            continue
        host = hosts[info["hostapi"]]["name"]
        if host == "Windows WDM-KS":
            continue
        item = _endpoint(info, host, index)
        item["direction"] = "render" if info["max_output_channels"] else "capture"
        candidates.append(item)
        name = info["name"].lower()
        if info["max_output_channels"] and ("cable input" in name or "vb-audio virtual cable" in name):
            if render is None or host == "Windows WASAPI":
                render = item
        if info["max_input_channels"] and "cable output" in name:
            if capture is None or host == "Windows WASAPI":
                capture = item
    installed = render is not None and capture is not None
    return {"installed": installed, "render": render, "capture": capture,
            "candidates": candidates, "pack": find_local_pack(),
            "vendorUrl": VENDOR_URL, "downloadUrl": DOWNLOAD_URL,
            "driverNote": DRIVER_NOTE, "error": "", "hint": hint(installed, find_local_pack())}


def hint(installed: bool, pack: dict[str, Any]) -> str:
    if installed:
        return "虚拟麦克风已就绪：本软件写入 CABLE Input，OBS / 会议软件选 CABLE Output。"
    if pack.get("present"):
        return ("未检测到虚拟设备。本机已有官方驱动包：点「打开官方安装器」，"
                "手动以管理员身份运行 Setup_x64.exe，装完重启再点「刷新状态」。")
    return ("未检测到虚拟设备。点「官网下载」获取 VB-CABLE 官方安装包，"
            "以管理员身份安装并重启，回来点「刷新状态」。")


def status() -> dict[str, Any]:
    """给前端用的完整状态；纯读取，不产生任何系统变更。"""
    return detect()


def open_installer() -> dict[str, Any]:
    """只打开官方安装包所在的文件夹并选中 Setup，不执行安装。"""
    pack = find_local_pack()
    if not pack.get("present"):
        return open_download()
    try:
        subprocess.Popen(["explorer", "/select,", pack["setup"]])
        return {"opened": pack["setup"], "mode": "folder",
                "hint": "请在打开的文件夹里右键 Setup_x64.exe → 以管理员身份运行；完成后重启电脑。"}
    except OSError:
        try:
            import os
            os.startfile(pack["dir"])  # noqa: S606
            return {"opened": pack["dir"], "mode": "folder", "hint": "请手动运行文件夹里的 Setup_x64.exe。"}
        except OSError as exc:
            return {"opened": "", "mode": "none", "error": str(exc)}


def open_download() -> dict[str, Any]:
    """打开官方下载页，由用户自行获取驱动。"""
    try:
        webbrowser.open(DOWNLOAD_URL)
        return {"opened": DOWNLOAD_URL, "mode": "download",
                "hint": "请在官网下载官方安装包，手动以管理员身份安装并重启电脑。"}
    except Exception as exc:
        return {"opened": "", "mode": "none", "error": str(exc)}
