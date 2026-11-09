"""统一的应用根目录与资源路径解析。

正常 Python 运行时，根目录是脚本所在目录；PyInstaller 打包成单文件 exe 后，
``__file__`` 会指向临时解压目录（``sys._MEIPASS``），而数据、模型、原生 DLL、
手机 APK 等都放在 exe 同级的真实磁盘上。这里统一返回「exe 所在目录」作为根。

用法：把各模块里的 ``ROOT = Path(__file__).resolve().parent`` 替换为
``from app_paths import ROOT``。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _app_root() -> Path:
    # PyInstaller 单文件模式：sys.frozen 为真，sys.executable 是 exe 路径。
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        return exe.parent
    # 正常解释器运行。
    return Path(__file__).resolve().parent


ROOT: Path = _app_root()
DATA: Path = ROOT / "data"
NATIVE: Path = ROOT / "native"
