"""Isolated VST3 scanning helper.

The service runs this file in a short-lived child process so a crashing or
hanging plugin module cannot take down the mixer service. Each discovered
plugin class (or scan failure) is printed as one JSON line on stdout.
"""
import ctypes
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DLL_PATH = ROOT / "native" / "bin" / "vst3host.dll"


def main() -> int:
    dirs = sys.argv[1:]
    if not dirs:
        print(json.dumps({"error": "缺少扫描目录"}))
        return 2
    if not DLL_PATH.is_file():
        print(json.dumps({"error": "未找到 native/bin/vst3host.dll，请先运行 native\\vst3host\\build.bat"}))
        return 2

    lib = ctypes.CDLL(str(DLL_PATH))
    callback_type = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_void_p)
    lib.vst_scan_directory.argtypes = [ctypes.c_char_p, callback_type, ctypes.c_void_p,
                                       ctypes.c_char_p, ctypes.c_int]
    lib.vst_scan_directory.restype = ctypes.c_int

    def emit(raw: bytes, _user) -> None:
        text = raw.decode("utf-8", "replace")
        print(text, flush=True)

    callback = callback_type(emit)
    total = 0
    for directory in dirs:
        buffer = ctypes.create_string_buffer(1024)
        count = lib.vst_scan_directory(str(directory).encode("utf-8"), callback, None, buffer, 1024)
        if count < 0:
            print(json.dumps({"error": buffer.value.decode("utf-8", "replace"),
                              "path": str(directory)}, ensure_ascii=False), flush=True)
        else:
            total += count
    print(json.dumps({"done": True, "classes": total}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
