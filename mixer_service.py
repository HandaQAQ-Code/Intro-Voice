"""Record Out mixer service: real audio I/O, AI voice chain and native VST3 hosting.

Runs headless and is controlled by the WinUI 3 front end (MixerUI) over the
named pipe ``\\.\pipe\RecordOutMixer`` using one JSON object per line.

Audio never crosses the control pipe: the Python service owns the WASAPI
streams, and plugins are processed in-process through native/bin/vst3host.dll.
"""
from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import queue
from pathlib import Path
import subprocess
import sys
import threading
import time

import numpy as np
import sounddevice as sd
import soxr

from mixer_dsp import DspChain, default_params as dsp_default_params, merge_params as dsp_merge_params
import market_service
import workshop_service
import mobile_mic
import virtual_device
from app_paths import ROOT, DATA

MIXER_DATA = DATA / "mixer"
PRESET_DIR = MIXER_DATA / "presets"
SETTINGS_FILE = MIXER_DATA / "settings.json"
DLL_PATH = ROOT / "native" / "bin" / "vst3host.dll"
LOG = logging.getLogger("mixer_service")


def find_mobile_apk() -> Path | None:
    for candidate in (ROOT / "tools" / "手机麦克风" / "IntroVoiceMic.apk",
                      ROOT / "mobile" / "IntroVoiceMic.apk",
                      ROOT / "mobile-android" / "IntroVoiceMic.apk"):
        if candidate.is_file():
            return candidate
    return None


def find_app_icon() -> Path | None:
    for candidate in (ROOT / "RecordOutMixer" / "Assets" / "app-icon.png",
                      ROOT / "MixerUI" / "Assets" / "app-icon.png"):
        if candidate.is_file():
            return candidate
    return None

# The portable build ships the noise-suppression and speaker-embedding models
# inside data/. Point both libraries at those copies so a fresh machine works
# without network access; a source checkout keeps using the normal user caches.
_bundled_models = ROOT / "data" / "models" / "dpdfnet"
if (_bundled_models / "dpdfnet2.onnx").is_file():
    os.environ.setdefault("DPDFNET_MODEL_DIR", str(_bundled_models))
_bundled_hf = ROOT / "data" / "runtime" / "hf"
if (_bundled_hf / "hub").is_dir():
    os.environ.setdefault("HF_HOME", str(_bundled_hf))

PIPE_NAME = r"\\.\pipe\RecordOutMixer"
PROTOCOL_VERSION = 1   # 控制协议版本；UI 与引擎不一致时明确报错而不是静默错位
PRESET_VERSION = 1     # 预设文件格式版本
CHAIN_RATE = 48000
CHAIN_BLOCK = 1920  # 40 ms, the processing block used by the AI chain


# ---------------------------------------------------------------------------
# VST3 chain wrapper (ctypes)
# ---------------------------------------------------------------------------
class Vst3Error(RuntimeError):
    pass


class Vst3Chain:
    def __init__(self) -> None:
        if not DLL_PATH.is_file():
            raise Vst3Error("未找到原生 VST3 宿主 DLL。请先运行 native\\vst3host\\build.bat。")
        self.lib = ctypes.CDLL(str(DLL_PATH))
        lib = self.lib
        lib.vst_chain_create.restype = ctypes.c_void_p
        lib.vst_chain_destroy.argtypes = [ctypes.c_void_p]
        lib.vst_chain_add.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
                                      ctypes.c_char_p, ctypes.c_int]
        lib.vst_chain_add.restype = ctypes.c_int
        lib.vst_chain_remove.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        lib.vst_chain_remove.restype = ctypes.c_int
        lib.vst_chain_move.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_char_p, ctypes.c_int]
        lib.vst_chain_count.argtypes = [ctypes.c_void_p]
        lib.vst_chain_count.restype = ctypes.c_int
        lib.vst_chain_set_bypass.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        lib.vst_chain_is_bypass.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.vst_chain_is_bypass.restype = ctypes.c_int
        lib.vst_chain_info.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        lib.vst_chain_info.restype = ctypes.c_int
        lib.vst_chain_param_count.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.vst_chain_param_count.restype = ctypes.c_int
        lib.vst_chain_param_info.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                             ctypes.c_char_p, ctypes.c_int]
        lib.vst_chain_param_info.restype = ctypes.c_int
        lib.vst_chain_get_param.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        lib.vst_chain_get_param.restype = ctypes.c_int
        lib.vst_chain_set_param.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_double]
        lib.vst_chain_set_param.restype = ctypes.c_int
        lib.vst_chain_setup.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_char_p, ctypes.c_int]
        lib.vst_chain_setup.restype = ctypes.c_int
        lib.vst_chain_start.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        lib.vst_chain_start.restype = ctypes.c_int
        lib.vst_chain_stop.argtypes = [ctypes.c_void_p]
        lib.vst_chain_process.argtypes = [ctypes.c_void_p,
                                          ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float),
                                          ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float),
                                          ctypes.c_int]
        lib.vst_chain_process.restype = ctypes.c_int
        lib.vst_chain_latency.argtypes = [ctypes.c_void_p]
        lib.vst_chain_latency.restype = ctypes.c_int
        lib.vst_chain_last_error.argtypes = [ctypes.c_void_p]
        lib.vst_chain_last_error.restype = ctypes.c_char_p
        lib.vst_chain_is_faulted.argtypes = [ctypes.c_void_p]
        lib.vst_chain_is_faulted.restype = ctypes.c_int
        lib.vst_chain_clear_fault.argtypes = [ctypes.c_void_p]
        lib.vst_chain_open_editor.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        lib.vst_chain_open_editor.restype = ctypes.c_int64
        lib.vst_chain_close_editor.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.vst_chain_editor_open.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.vst_chain_editor_open.restype = ctypes.c_int
        lib.vst_chain_get_state.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_int)]
        lib.vst_chain_get_state.restype = ctypes.c_int
        lib.vst_chain_set_state.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                            ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        lib.vst_chain_set_state.restype = ctypes.c_int
        lib.vst_free.argtypes = [ctypes.c_void_p]
        self.handle = lib.vst_chain_create()
        if not self.handle:
            raise Vst3Error("原生 VST3 宿主初始化失败")
        self.block = CHAIN_BLOCK
        self._out_l = np.zeros(CHAIN_BLOCK, np.float32)
        self._out_r = np.zeros(CHAIN_BLOCK, np.float32)
        self._lock = threading.Lock()

    def close(self) -> None:
        if self.handle:
            with self._lock:
                self.lib.vst_chain_stop(self.handle)
                self.lib.vst_chain_destroy(self.handle)
                self.handle = None

    def _err(self) -> str:
        data = self.lib.vst_chain_last_error(self.handle)
        return data.decode("utf-8", "replace") if data else "未知错误"

    def add(self, path: str, class_id: str) -> int:
        buf = ctypes.create_string_buffer(512)
        index = self.lib.vst_chain_add(self.handle, path.encode(), class_id.encode(), buf, 512)
        if index < 0:
            raise Vst3Error(buf.value.decode("utf-8", "replace"))
        return index

    def remove(self, index: int) -> None:
        buf = ctypes.create_string_buffer(512)
        if self.lib.vst_chain_remove(self.handle, index, buf, 512) < 0:
            raise Vst3Error(buf.value.decode("utf-8", "replace"))

    def move(self, src: int, dst: int) -> None:
        buf = ctypes.create_string_buffer(512)
        if self.lib.vst_chain_move(self.handle, src, dst, buf, 512) < 0:
            raise Vst3Error(buf.value.decode("utf-8", "replace"))

    def count(self) -> int:
        return self.lib.vst_chain_count(self.handle)

    def set_bypass(self, index: int, bypass: bool) -> None:
        if self.lib.vst_chain_set_bypass(self.handle, index, 1 if bypass else 0) < 0:
            raise Vst3Error("无法切换插件旁通状态")

    def info(self, index: int) -> dict:
        buf = ctypes.create_string_buffer(1024)
        if self.lib.vst_chain_info(self.handle, index, buf, 1024) < 0:
            raise Vst3Error("无法读取插件信息")
        return json.loads(buf.value)

    def param_count(self, index: int) -> int:
        return self.lib.vst_chain_param_count(self.handle, index)

    def params(self, index: int) -> list[dict]:
        out = []
        buf = ctypes.create_string_buffer(2048)
        for i in range(max(0, self.param_count(index))):
            if self.lib.vst_chain_param_info(self.handle, index, i, buf, 2048) == 0:
                item = json.loads(buf.value)
                item["flags"] = int(item.get("flags", 0))
                item["isAutomation"] = bool(item["flags"] & 1)  # kVstParameterIsAutomable
                item["isReadOnly"] = bool(item["flags"] & 2)    # kVstParameterIsReadOnly
                out.append(item)
        return out

    def get_param(self, index: int, param: int) -> float:
        return self.lib.vst_chain_get_param(self.handle, index, param)

    def set_param(self, index: int, param: int, value: float) -> None:
        if self.lib.vst_chain_set_param(self.handle, index, param, float(value)) < 0:
            raise Vst3Error("无法设置插件参数")

    def setup(self, sample_rate: int = CHAIN_RATE, block: int = CHAIN_BLOCK) -> None:
        buf = ctypes.create_string_buffer(512)
        if self.lib.vst_chain_setup(self.handle, sample_rate, block, buf, 512) < 0:
            raise Vst3Error(buf.value.decode("utf-8", "replace"))
        self.block = block

    def start(self) -> None:
        buf = ctypes.create_string_buffer(512)
        if self.lib.vst_chain_start(self.handle, buf, 512) < 0:
            raise Vst3Error(buf.value.decode("utf-8", "replace"))

    def stop(self) -> None:
        self.lib.vst_chain_stop(self.handle)

    def latency(self) -> int:
        return int(self.lib.vst_chain_latency(self.handle))

    def faulted(self) -> bool:
        return bool(self.lib.vst_chain_is_faulted(self.handle))

    def clear_fault(self) -> None:
        self.lib.vst_chain_clear_fault(self.handle)

    def process(self, left: np.ndarray, right: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        frames = min(len(left), len(right))
        outs_l, outs_r = [], []
        for start in range(0, frames, self.block):
            n = min(self.block, frames - start)
            seg_l = np.ascontiguousarray(left[start:start + n], dtype=np.float32)
            seg_r = np.ascontiguousarray(right[start:start + n], dtype=np.float32)
            self._out_l[:n] = 0
            self._out_r[:n] = 0
            with self._lock:
                code = self.lib.vst_chain_process(
                    self.handle,
                    seg_l.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                    seg_r.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                    self._out_l[:n].ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                    self._out_r[:n].ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                    n)
            if code != 0:
                raise Vst3Error(self._err())
            outs_l.append(self._out_l[:n].copy())
            outs_r.append(self._out_r[:n].copy())
        if not outs_l:
            return np.zeros(0, np.float32), np.zeros(0, np.float32)
        return np.concatenate(outs_l), np.concatenate(outs_r)

    def open_editor(self, index: int) -> int:
        buf = ctypes.create_string_buffer(512)
        hwnd = self.lib.vst_chain_open_editor(self.handle, index, buf, 512)
        if hwnd < 0:
            raise Vst3Error(buf.value.decode("utf-8", "replace"))
        return int(hwnd)

    def close_editor(self, index: int) -> None:
        self.lib.vst_chain_close_editor(self.handle, index)

    def editor_open(self, index: int) -> bool:
        return bool(self.lib.vst_chain_editor_open(self.handle, index))

    def get_state(self, index: int) -> bytes:
        data = ctypes.c_void_p()
        size = ctypes.c_int()
        if self.lib.vst_chain_get_state(self.handle, index, ctypes.byref(data), ctypes.byref(size)) < 0:
            raise Vst3Error("无法读取插件状态")
        try:
            return ctypes.string_at(data, size.value)
        finally:
            self.lib.vst_free(data)

    def set_state(self, index: int, blob: bytes) -> None:
        if not blob:
            return
        buf = ctypes.create_string_buffer(512)
        array = (ctypes.c_char * len(blob)).from_buffer_copy(blob)
        if self.lib.vst_chain_set_state(self.handle, index, array, len(blob), buf, 512) < 0:
            raise Vst3Error(buf.value.decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# audio helpers
# ---------------------------------------------------------------------------
class StereoResampler:
    def __init__(self, source: int, target: int, channels: int):
        self.streams = ([soxr.ResampleStream(source, target, 1, dtype="float32", quality="MQ")
                         for _ in range(channels)] if source != target else None)

    def process(self, audio: np.ndarray) -> np.ndarray:
        if not len(audio):
            return np.zeros((0, audio.shape[1]), np.float32)
        if self.streams is None:
            return audio.astype(np.float32, copy=False)
        parts = [self.streams[c].resample_chunk(np.ascontiguousarray(audio[:, c]))
                 for c in range(audio.shape[1])]
        length = min(len(p) for p in parts)
        return np.stack([p[:length] for p in parts], axis=1).astype(np.float32)


class StereoBuffer:
    """Bounded output FIFO for stereo float32 audio."""

    def __init__(self, rate: int, prefill_ms: float):
        self._blocks = []
        self._offset = 0
        self.size = 0
        self.maximum = round(rate * 0.20)
        self.prefill = max(1, round(rate * prefill_ms / 1000))
        self.ready = False
        self.underruns = 0
        self.trims = 0
        self.lock = threading.Lock()

    def put(self, audio: np.ndarray) -> None:
        if not len(audio):
            return
        with self.lock:
            self._blocks.append(np.asarray(audio, np.float32))
            self.size += len(audio)
            if self.size > self.maximum:
                self.trims += 1
                while self.size > self.prefill and len(self._blocks) > 1:
                    self.size -= len(self._blocks[0]) - self._offset
                    del self._blocks[0]
                    self._offset = 0

    def fill(self, outdata: np.ndarray) -> None:
        outdata.fill(0)
        with self.lock:
            if not self.ready:
                if self.size < self.prefill:
                    return
                self.ready = True
            written = 0
            frames = len(outdata)
            channels = outdata.shape[1]
            while self._blocks and written < frames:
                block = self._blocks[0]
                count = min(frames - written, len(block) - self._offset)
                for c in range(channels):
                    outdata[written:written + count, c] = block[self._offset:self._offset + count,
                                                                min(c, block.shape[1] - 1)]
                self._offset += count
                self.size -= count
                written += count
                if self._offset == len(block):
                    del self._blocks[0]
                    self._offset = 0
            if written < frames:
                self.underruns += 1
                self.ready = False

    def clear(self) -> None:
        with self.lock:
            self._blocks.clear()
            self.size = self._offset = 0
            self.ready = False


class PeakLimiter:
    def __init__(self, rate: int, ceiling: float = 0.98):
        self.rate = rate
        self.ceiling = ceiling
        self.gain = 1.0

    def process(self, audio: np.ndarray) -> np.ndarray:
        if not len(audio):
            return audio
        # Stereo input is (frames, channels) while the envelope is per frame, so
        # every frame-based curve has to be reshaped to (frames, 1). Multiplying
        # (1920, 2) by (1920,) directly is what raised the NumPy broadcast error.
        frames = np.asarray(audio, np.float32)
        single = frames.ndim == 1
        x = frames[:, None] if single else frames
        peak = float(np.max(np.abs(x)))
        if self.gain >= 0.9999 and peak <= self.ceiling:
            return audio
        required = np.minimum(1.0, self.ceiling / np.maximum(np.abs(x), 1e-9))
        decay = np.exp(-np.arange(1, len(x) + 1, dtype=np.float64) / (self.rate * 0.10))[:, None]
        deficit = np.maximum.accumulate((1.0 - required.astype(np.float64)) / decay, axis=0)
        gains = 1.0 - np.maximum(deficit, 1.0 - self.gain) * decay
        # Keep one gain for the whole frame so a transient never shifts the
        # stereo image, and remember the most conservative channel value.
        self.gain = float(gains[-1].min())
        limited = (x * gains.astype(np.float32)).astype(np.float32)
        return limited[:, 0] if single else limited


def pick_output_format(device: int) -> tuple[int, int]:
    info = sd.query_devices(device)
    maximum = int(info["max_output_channels"])
    for channels in (2, 1):
        if channels > maximum:
            continue
        try:
            sd.check_output_settings(device=device, samplerate=CHAIN_RATE, channels=channels,
                                     dtype="float32", extra_settings=_device_options(device))
            return CHAIN_RATE, channels
        except sd.PortAudioError:
            continue
    raise RuntimeError(f"输出设备不支持 48 kHz 音频：{info['name']}")


def _device_options(device: int):
    info = sd.query_devices(device)
    if sd.query_hostapis(info["hostapi"])["name"] == "Windows WASAPI":
        return sd.WasapiSettings(exclusive=False, auto_convert=True)
    return None


def device_label(index: int) -> str:
    info = sd.query_devices(index)
    host = sd.query_hostapis(info["hostapi"])["name"]
    return f"{index}: {info['name']} [{host}]"


# ---------------------------------------------------------------------------
# live machine metrics (CPU / RAM / GPU)
# ---------------------------------------------------------------------------
class SystemMonitor:
    """Samples global CPU, RAM and (when present) NVIDIA GPU load once a second.

    All values are read from the OS directly so the packaged build has no extra
    dependency: GetSystemTimes / GlobalMemoryStatusEx through ctypes and
    nvidia-smi for the GPU. Sampling happens on a daemon thread, so the audio
    thread never waits on performance counters.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._metrics = {"cpu": 0.0, "ram": 0.0, "gpu": -1.0, "gpuName": "", "ramUsedMb": 0}
        self._stop = threading.Event()
        self._prev = None
        self._gpu_name = ""
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=False)
        self._thread = threading.Thread(target=self._loop, name="system-monitor", daemon=True)
        self._thread.start()

    def _cpu_times(self):
        idle, kernel, user = (ctypes.c_uint64(), ctypes.c_uint64(), ctypes.c_uint64())
        if not self._kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel),
                                             ctypes.byref(user)):
            return None
        return idle.value, kernel.value, user.value

    def _memory(self):
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32),
                        ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                        ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                        ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                        ("ullAvailExtendedVirtual", ctypes.c_uint64)]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not self._kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return 0.0, 0
        used = int((status.ullTotalPhys - status.ullAvailPhys) / (1024 * 1024))
        return float(status.dwMemoryLoad), used

    def _gpu(self) -> tuple[float, str]:
        try:
            output = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,name", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            return -1.0, ""
        line = output.stdout.strip().splitlines()
        if not line:
            return -1.0, ""
        parts = [p.strip() for p in line[0].split(",")]
        try:
            return float(parts[0]), parts[-1]
        except (ValueError, IndexError):
            return -1.0, ""

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                times = self._cpu_times()
                cpu = 0.0
                if times and self._prev:
                    idle, kernel, user = times
                    p_idle, p_kernel, p_user = self._prev
                    total = (kernel - p_kernel) + (user - p_user)
                    if total > 0:
                        cpu = max(0.0, min(100.0, 100.0 * (1.0 - (idle - p_idle) / total)))
                self._prev = times
                ram, used = self._memory()
                gpu, name = self._gpu()
                if name:
                    self._gpu_name = name
                with self._lock:
                    self._metrics = {"cpu": round(cpu, 1), "ram": round(ram, 1),
                                     "gpu": gpu, "gpuName": self._gpu_name,
                                     "ramUsedMb": used}
            except Exception:  # never let monitoring kill the audio service
                LOG.debug("系统监控采样失败", exc_info=True)
            self._stop.wait(1.0)

    def metrics(self) -> dict:
        with self._lock:
            return dict(self._metrics)

    def close(self) -> None:
        self._stop.set()


def gpu_capability() -> tuple[bool, str]:
    """Whether the GPU tier can run here, plus a short human readable reason."""
    try:
        import torch
    except Exception:
        return False, "当前环境未安装 CUDA 版 PyTorch"
    try:
        if not torch.cuda.is_available():
            return False, "未检测到可用的 NVIDIA CUDA 设备"
        return True, str(torch.cuda.get_device_name(0))
    except Exception as exc:
        return False, f"CUDA 初始化失败：{exc}"


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------
class EngineOptions:
    def __init__(self):
        self.gain_db = 0.0
        self.floor_db = -55.0
        self.denoise = False
        self.sr_strength = 0.7
        self.muted = False
        self.latency_mode = "balanced"


class _VirtualInput:
    """把手机麦克风的环形缓冲伪装成输入流，按块节奏喂给处理线程。"""

    def __init__(self, engine: "MixerEngine", pending: queue.Queue,
                 frames: int, rate: int):
        self.engine = engine
        self.pending = pending
        self.frames = max(1, int(frames))
        self.period = self.frames / float(rate)
        self.active = True
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "_VirtualInput":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.active = False
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        nxt = time.monotonic()
        while not self._stop.is_set() and not self.engine.stop_event.is_set():
            mobile = self.engine.mobile
            if mobile is None:
                audio = np.zeros(self.frames, dtype=np.float32)
            else:
                audio = mobile.read(self.frames)
            try:
                self.pending.put_nowait((time.monotonic(), audio[:, None]))
            except queue.Full:
                try:
                    self.pending.get_nowait()
                except queue.Empty:
                    pass
                self.engine.drops += 1
                try:
                    self.pending.put_nowait((time.monotonic(), audio[:, None]))
                except queue.Full:
                    pass
            nxt += self.period
            delay = nxt - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                nxt = time.monotonic()


class MixerEngine:
    def __init__(self, chain: Vst3Chain, events: queue.Queue, mobile=None):
        self.chain = chain
        self.events = events
        self.mobile = mobile          # MobileMic：手机麦克风环形缓冲
        self.input_source = "device"
        self.options = EngineOptions()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.buffer: StereoBuffer | None = None
        self.lock = threading.Lock()
        self.running = False
        self.loading = False
        self.fault_text = ""
        self.backend = ""
        self.ai_mode = "off"
        self.rtf = 0.0
        self.input_db = -90.0
        self.output_db = -90.0
        self.drops = 0
        self.devicErrors = 0
        self.input_rate = 0
        self.output_rate = CHAIN_RATE
        self.pipeline = None
        self._pipeline_ai_mode = "off"
        self.latency_samples = 0
        self.dsp = DspChain(CHAIN_RATE, 2)
        self.dsp_params = dsp_default_params()
        self.workshop = workshop_service.WorkshopRunner(CHAIN_RATE)
        self.workshop_error = ""
        self.spectrum = [0.0] * 32

    # -- lifecycle ---------------------------------------------------------
    def start(self, cfg: dict) -> None:
        if self.thread and self.thread.is_alive():
            raise RuntimeError("音频引擎已在运行")
        self.ai_mode = cfg.get("aiMode", "off")
        self.options.gain_db = float(cfg.get("gainDb", 0.0))
        self.options.floor_db = float(cfg.get("floorDb", -55.0))
        self.options.denoise = bool(cfg.get("denoise", False))
        self.options.sr_strength = max(0.0, min(1.0, float(cfg.get("srStrength", 0.7))))
        self.options.latency_mode = cfg.get("latencyMode", "balanced")
        self.options.muted = False
        self.dsp_params = cfg.get("dsp") or dsp_default_params()
        self.stop_event.clear()
        self.loading = True
        self.fault_text = ""
        self.running = False
        self.buffer = None
        self.thread = threading.Thread(target=self._run, args=(cfg,), daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.buffer:
            self.buffer.clear()

    def _status(self, text: str) -> None:
        try:
            self.events.put_nowait(("status", text))
        except queue.Full:
            pass

    def _run(self, cfg: dict) -> None:
        from live_voice import (Models, Pipeline, PROFILE, validate_sample, device_format,
                                open_audio_stream, windows_audio_context)
        import voxlock

        input_device = int(cfg["inputDevice"])
        output_device = int(cfg["outputDevice"])
        try:
            with windows_audio_context():
                self._status("准备音频设备…")
                self.input_source = str(cfg.get("inputSource") or "device")
                if self.input_source == "mobile":
                    self.input_rate, input_channels = CHAIN_RATE, 1
                else:
                    self.input_rate, input_channels = device_format(input_device, "input")
                self.output_rate, output_channels = pick_output_format(output_device)
                chain_latency = 0
                if self.chain.count() > 0:
                    self.chain.setup(CHAIN_RATE, CHAIN_BLOCK)
                    self.chain.start()
                    chain_latency = self.chain.latency()
                self.latency_samples = chain_latency
                self.dsp.set_params(self.dsp_params)

                models = None
                sample = None
                if self.ai_mode != "off":
                    sample = validate_sample(voxlock.load_audio(PROFILE))
                    models = Models(None, superres=(self.ai_mode != "cpu"), gpu=(self.ai_mode == "gpu"))
                    models.latency_mode = self.options.latency_mode
                    models.load(self._status)
                    self.backend = models.backend_name
                    self.pipeline = Pipeline(models, sample, self.input_rate, CHAIN_RATE)
                    self._pipeline_ai_mode = self.ai_mode
                    block_ms = 40
                    if self.ai_mode == "gpu":
                        from gpu_super_resolution import LATENCY_PROFILES
                        block_ms = LATENCY_PROFILES[self.options.latency_mode]["block_ms"]
                else:
                    self.backend = "全带宽旁通（未使用 AI / 未重采样到 16 kHz）"
                    block_ms = 40

                self.buffer = StereoBuffer(CHAIN_RATE, 80 + chain_latency * 1000 / CHAIN_RATE)
                pending: queue.Queue = queue.Queue(maxsize=5)
                resampler = StereoResampler(self.input_rate, CHAIN_RATE, input_channels)
                limiter = PeakLimiter(CHAIN_RATE)
                chunk_size = round(self.input_rate * block_ms / 1000)

                def capture(indata, frames, timing, status):
                    if self.stop_event.is_set():
                        raise sd.CallbackStop
                    if status:
                        self.devicErrors += 1
                    try:
                        pending.put_nowait((time.monotonic(), indata.copy()))
                    except queue.Full:
                        try:
                            pending.get_nowait()
                        except queue.Empty:
                            pass
                        self.drops += 1
                        try:
                            pending.put_nowait((time.monotonic(), indata.copy()))
                        except queue.Full:
                            pass

                def playback(outdata, frames, timing, status):
                    if status:
                        self.devicErrors += 1
                    if self.stop_event.is_set():
                        outdata.fill(0)
                        raise sd.CallbackStop
                    if self.buffer is None:
                        outdata.fill(0)
                        return
                    self.buffer.fill(outdata)
                    if self.options.muted or self.fault_text:
                        outdata.fill(0)
                    # Report the level actually leaving the output endpoint, so a
                    # muted or faulted engine cannot show a healthy meter.
                    self.output_db = 20 * np.log10(float(np.sqrt(np.mean(outdata ** 2))) + 1e-9)

                input_ctx = (_VirtualInput(self, pending, chunk_size, self.input_rate)
                             if self.input_source == "mobile" else
                             open_audio_stream("input", device=input_device, samplerate=self.input_rate,
                                               channels=input_channels, dtype="float32",
                                               blocksize=chunk_size, callback=capture))
                with open_audio_stream("output", device=output_device, samplerate=CHAIN_RATE,
                                       channels=output_channels, dtype="float32", blocksize=480,
                                       callback=playback) as output_stream:
                    with input_ctx as input_stream:
                        self.running = True
                        self.loading = False
                        source_label = ("手机麦克风（Wi-Fi）" if self.input_source == "mobile"
                                        else device_label(input_device))
                        self._status(f"运行中：{source_label} → {device_label(output_device)}")
                        last_audio = time.monotonic()
                        stalls = 0
                        previous_drops = 0
                        while not self.stop_event.is_set():
                            if not input_stream.active or not output_stream.active:
                                raise RuntimeError("音频设备已断开或停止")
                            try:
                                captured_at, audio = pending.get(timeout=0.10)
                            except queue.Empty:
                                if time.monotonic() - last_audio > 3:
                                    raise RuntimeError("麦克风连续 3 秒未返回音频")
                                continue
                            last_audio = time.monotonic()
                            if self.drops != previous_drops or last_audio - captured_at > 0.24:
                                previous_drops = self.drops
                                stalls += 1
                                if self.buffer:
                                    self.buffer.clear()
                                if stalls >= 3:
                                    raise RuntimeError("处理速度持续不足，已静音停止")
                                while not pending.empty():
                                    try:
                                        pending.get_nowait()
                                    except queue.Empty:
                                        break
                                limiter = PeakLimiter(CHAIN_RATE)
                                if self.ai_mode != "off":
                                    self.pipeline = Pipeline(models, sample, self.input_rate, CHAIN_RATE)
                                continue
                            started = time.perf_counter()
                            processed = self._process_block(audio, resampler, limiter, models, sample)
                            elapsed = time.perf_counter() - started
                            self.rtf = 0.9 * self.rtf + 0.1 * elapsed / max(1e-6, len(audio) / self.input_rate)
                            self.input_db = 20 * np.log10(float(np.sqrt(np.mean(audio ** 2))) + 1e-9)
                            if len(processed):
                                self._update_spectrum(processed)
                                self.buffer.put(processed)
        except Exception as exc:
            LOG.exception("音频处理停止")
            self.fault_text = str(exc)
            self.loading = False
            try:
                self.events.put_nowait(("error", str(exc)))
            except queue.Full:
                pass
        finally:
            self.stop_event.set()
            try:
                if self.chain.handle:
                    self.chain.stop()
            except Exception:
                LOG.exception("停止插件链失败")
            self.running = False
            self.loading = False
            if self.buffer:
                self.buffer.clear()
            try:
                self.events.put_nowait(("stopped", None))
            except queue.Full:
                pass


    SPECTRUM_BANDS = 32
    SPECTRUM_EDGES = np.geomspace(40.0, 16000.0, 33)

    def _update_spectrum(self, block: np.ndarray) -> None:
        """对输出块做一次短 FFT，得到 32 段对数频带电平（0..1），供界面绘制。"""
        try:
            mono = block.mean(axis=1)
            if len(mono) < 512:
                return
            window = mono[-1024:] * np.hanning(min(1024, len(mono)))
            spec = np.abs(np.fft.rfft(window))
            freqs = np.fft.rfftfreq(len(window), 1.0 / CHAIN_RATE)
            level = np.zeros(self.SPECTRUM_BANDS)
            for i in range(self.SPECTRUM_BANDS):
                mask = (freqs >= self.SPECTRUM_EDGES[i]) & (freqs < self.SPECTRUM_EDGES[i + 1])
                if mask.any():
                    level[i] = float(spec[mask].mean())
            norm = np.clip((20.0 * np.log10(level + 1e-9) + 72.0) / 66.0, 0.0, 1.0)
            prev = np.asarray(self.spectrum)
            self.spectrum = (0.55 * prev + 0.45 * norm).round(4).tolist()
        except Exception:
            pass

    def _process_block(self, audio, resampler, limiter, models, sample):
        if self.ai_mode != "off":
            mono = audio[:, 0]
            y = self.pipeline.process(mono, self.options.denoise, self.options.floor_db,
                                      self.options.gain_db, self.options.sr_strength)
            if not len(y):
                return np.zeros((0, 2), np.float32)
            if not np.isfinite(y).all():
                raise RuntimeError("AI 模型输出异常，已静音")
            stereo = np.stack([y, y], axis=1)
        else:
            stereo = resampler.process(audio)
            if stereo.shape[1] == 1:
                stereo = np.repeat(stereo, 2, axis=1)
            stereo = stereo[:, :2]
            stereo = stereo * (10 ** (self.options.gain_db / 20))
        # 内置效果器：HPF/LPF → Gate → 压缩 → EQ，位于 VST3 链之前、输出限幅之前。
        if len(stereo):
            stereo = self.dsp.process(stereo)
        # 创意工坊插件：用户脚本，位于内置效果之后、VST3 之前。
        if len(stereo):
            stereo = self.workshop.process(stereo)
        if self.chain.count() > 0:
            left, right = self.chain.process(stereo[:, 0], stereo[:, 1])
            stereo = np.stack([left, right], axis=1)
        if not np.isfinite(stereo).all():
            raise RuntimeError("音频处理出现非有限值，已静音")
        return limiter.process(stereo)


# ---------------------------------------------------------------------------
# named pipe server
# ---------------------------------------------------------------------------
class PipeServer:
    PIPE_ACCESS_DUPLEX = 0x00000003
    PIPE_TYPE_BYTE = 0x00000000
    PIPE_READMODE_BYTE = 0x00000000
    PIPE_WAIT = 0x00000000
    ERROR_PIPE_CONNECTED = 535

    def __init__(self, name: str):
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.CreateNamedPipeW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32,
                                                   ctypes.c_uint32, ctypes.c_uint32,
                                                   ctypes.c_uint32, ctypes.c_uint32,
                                                   ctypes.c_uint32, ctypes.c_void_p]
        self.kernel32.CreateNamedPipeW.restype = ctypes.c_void_p
        self.kernel32.ConnectNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.kernel32.ConnectNamedPipe.restype = ctypes.c_int
        self.kernel32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                           ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        self.kernel32.ReadFile.restype = ctypes.c_int
        self.kernel32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        self.kernel32.WriteFile.restype = ctypes.c_int
        self.kernel32.DisconnectNamedPipe.argtypes = [ctypes.c_void_p]
        self.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self.name = name
        self.handle = None

    def _create(self):
        handle = self.kernel32.CreateNamedPipeW(self.name, self.PIPE_ACCESS_DUPLEX,
                                                self.PIPE_TYPE_BYTE | self.PIPE_READMODE_BYTE | self.PIPE_WAIT,
                                                1, 65536, 65536, 0, None)
        if not handle:
            raise OSError("无法创建命名管道")
        self.handle = handle

    def accept(self) -> bool:
        if self.handle is None:
            self._create()
        ok = self.kernel32.ConnectNamedPipe(self.handle, None)
        if not ok and ctypes.get_last_error() != self.ERROR_PIPE_CONNECTED:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None
            return False
        return True

    def read_line_parts(self, buffer: bytearray) -> list[bytes] | None:
        # Byte-mode ReadFile returns as soon as buffered data exists. Never call it
        # twice before handling the first chunk, or a request is left unanswered
        # until the client happens to send more bytes.
        result = []
        chunk = ctypes.create_string_buffer(4096)
        read = ctypes.c_uint32(0)
        if not self.kernel32.ReadFile(self.handle, chunk, 4096, ctypes.byref(read), None):
            return None
        if read.value == 0:
            return None
        buffer.extend(chunk.raw[:read.value])
        while b"\n" in buffer:
            line, _, rest = bytes(buffer).partition(b"\n")
            buffer[:] = rest
            if line.strip():
                result.append(line)
        return result

    def write(self, data: bytes) -> bool:
        written = ctypes.c_uint32(0)
        chunk = ctypes.create_string_buffer(data)
        if not self.kernel32.WriteFile(self.handle, chunk, len(data), ctypes.byref(written), None):
            return False
        return written.value == len(data)

    def disconnect(self):
        if self.handle:
            self.kernel32.DisconnectNamedPipe(self.handle)
            self.kernel32.CloseHandle(self.handle)
            self.handle = None


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------
def standard_vst3_dirs() -> list[str]:
    dirs = [Path(os.environ.get("CommonProgramFiles", r"C:\Program Files\Common Files")) / "VST3",
            Path(os.environ.get("CommonProgramFiles(x86)", r"C:\Program Files (x86)\Common Files")) / "VST3",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Common" / "VST3"]
    return [str(p) for p in dirs if p.is_dir()]


def worker_python() -> str:
    """Absolute interpreter for helper processes (scan worker).

    sys.executable can be a stale path when the service itself was launched
    through a relative path, so verify it before use and fall back to the
    interpreter that owns the running installation.
    """
    exe = Path(sys.executable) if sys.executable else None
    if exe is not None and exe.is_absolute() and exe.is_file():
        return str(exe)
    fallback = Path(sys.base_prefix) / "python.exe"
    return str(fallback) if fallback.is_file() else "python.exe"


def _scan_worker_command(dirs: list[str]) -> list[str]:
    """构造插件扫描子进程的命令行。

    正常解释器运行：``<python> vst3_scan_worker.py <dirs>``；
    PyInstaller 单文件：``<mixer_service.exe> --scan-worker <dirs>``，
    复用同一个 exe 并在入口分支里处理扫描逻辑。
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--scan-worker", *dirs]
    worker = ROOT / "vst3_scan_worker.py"
    return [worker_python(), str(worker), *dirs]


def scan_plugins(dirs: list[str]) -> dict:
    max_timeout = 120
    # Redirect child output to a file instead of a pipe: some plugin modules spawn
    # helper processes that inherit stdout and would otherwise keep a pipe open
    # long after the scanner itself has exited.
    output_path = MIXER_DATA / "scan_output.jsonl"
    plugins, errors = [], []
    try:
        with open(output_path, "wb") as handle:
            proc = subprocess.run(_scan_worker_command(dirs),
                                  stdout=handle, stderr=subprocess.STDOUT, timeout=max_timeout,
                                  cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        return {"plugins": [], "errors": [{"error": f"插件扫描超过 {max_timeout} 秒，已终止"}]}
    raw_text = output_path.read_text(encoding="utf-8", errors="replace")
    for raw in raw_text.splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if item.get("error"):
            errors.append(item)
        elif item.get("isEffect"):
            plugins.append(item)
    seen = set()
    unique = []
    for item in plugins:
        key = (item.get("path", "").lower(), item.get("classId", ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    unique.sort(key=lambda p: (p.get("name", "").lower(),
                               p.get("vendor", "").lower(), p.get("path", "")))
    if proc.returncode != 0:
        errors.append({"error": f"扫描子进程退出码 {proc.returncode}",
                       "detail": raw_text.splitlines()[-1][:200] if raw_text else ""})
    return {"plugins": unique, "errors": errors}


class Service:
    def __init__(self):
        MIXER_DATA.mkdir(parents=True, exist_ok=True)
        PRESET_DIR.mkdir(parents=True, exist_ok=True)
        self.events: queue.Queue = queue.Queue(maxsize=200)
        self.messages: list[str] = []
        self.chain = None
        self.chain_error = ""
        try:
            self.chain = Vst3Chain()
        except Exception as exc:
            self.chain_error = str(exc)
            LOG.exception("原生宿主加载失败")
        self.mobile = mobile_mic.MobileMic(CHAIN_RATE)
        self.mic_server = mobile_mic.MicServer(self.mobile, apk_path=find_mobile_apk(),
                                               icon_path=find_app_icon())
        self.engine = MixerEngine(self.chain, self.events, self.mobile) if self.chain else None
        self.monitor = SystemMonitor()
        self.should_stop = False
        self.settings = self._load_settings()
        if self.mobile:
            cfg = self.settings["mobileMic"]
            self.mobile.configure(**{k: cfg[k] for k in ("gainDb", "hpfHz", "gateDb", "jitterMs")})
            self.mic_server._requested = int(cfg.get("port") or mobile_mic.DEFAULT_PORT)
        self.scan_cache: list[dict] = []
        self.running = False
        self.progress: dict | None = None

    # -- helpers -----------------------------------------------------------
    def _load_settings(self) -> dict:
        defaults = {"input": "", "output": "", "inputSource": "device",
                    "aiMode": "off", "denoise": False,
                    "gainDb": 0.0, "floorDb": -55.0, "srStrength": 0.7, "latencyMode": "balanced",
                    "dsp": dsp_default_params(),
                    "workshop": {"order": [], "enabled": {}, "params": {}},
                    "mobileMic": {"gainDb": 0.0, "hpfHz": 80.0, "gateDb": -62.0,
                                  "jitterMs": 80.0, "port": mobile_mic.DEFAULT_PORT}}
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                defaults.update({k: v for k, v in saved.items() if k in defaults})
        except (OSError, ValueError):
            pass
        # 嵌套 DSP 参数按版本合并，旧配置缺键时补默认值。
        defaults["dsp"] = dsp_merge_params(dsp_default_params(), defaults.get("dsp") or {})
        shop = defaults.get("workshop")
        if not isinstance(shop, dict):
            shop = {}
        defaults["workshop"] = {
            "order": [x for x in (shop.get("order") or []) if isinstance(x, str)],
            "enabled": dict(shop.get("enabled") or {}),
            "params": dict(shop.get("params") or {}),
        }
        mic = defaults.get("mobileMic")
        if not isinstance(mic, dict):
            mic = {}
        defaults["mobileMic"] = {
            "gainDb": float(mic.get("gainDb", 0.0)),
            "hpfHz": float(mic.get("hpfHz", 80.0)),
            "gateDb": float(mic.get("gateDb", -62.0)),
            "jitterMs": float(mic.get("jitterMs", 80.0)),
            "port": int(mic.get("port", mobile_mic.DEFAULT_PORT)),
        }
        return defaults

    def _save_settings(self) -> None:
        try:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            temp = SETTINGS_FILE.with_suffix(".pending")
            temp.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, SETTINGS_FILE)
        except OSError:
            LOG.exception("保存调音台设置失败")

    def _apply_workshop(self) -> list[str]:
        """把 settings.workshop 编译成可执行的阶段，运行中也能热更新。"""
        if not self.engine:
            return []
        stages, errors = workshop_service.build_stages(ROOT, self.settings.get("workshop"))
        self.engine.workshop.set_stages(stages)
        self.engine.workshop_error = "；".join(errors)
        return errors

    def _devices(self) -> dict:
        hosts = sd.query_hostapis()
        raw_devices = list(sd.query_devices())
        inputs, outputs = [], []
        for index, info in enumerate(raw_devices):
            host = hosts[info["hostapi"]]["name"]
            if host == "Windows WDM-KS":
                continue
            label = f"{index}: {info['name']} [{host}]"
            record = {"id": index, "label": label, "name": info["name"], "host": host,
                      "rate": int(info["default_samplerate"]),
                      "channels": int(info["max_input_channels"]),
                      "outChannels": int(info["max_output_channels"])}
            if info["max_input_channels"]:
                inputs.append(record)
            if info["max_output_channels"]:
                outputs.append(record)
        # Prefer WASAPI: it is the only host the engine opens with auto_convert,
        # and MME labels of the same endpoint would otherwise win by index order.
        cable_candidates = [o for o in outputs
                            if ("cable input" in o["name"].lower()
                                or "vb-audio virtual cable" in o["name"].lower()
                                or "cable in " in o["label"].lower())
                            and "16 ch" not in o["name"].lower()]
        cable = next((o["label"] for o in cable_candidates if o["host"] == "Windows WASAPI"), "")
        if not cable and cable_candidates:
            cable = cable_candidates[0]["label"]
        default_input = ""
        wasapi_inputs = [i for i in inputs if i["host"] == "Windows WASAPI"]
        try:
            default_index = int(sd.default.device[0])
            default_name = sd.query_devices(default_index)["name"]
            match = next((i["label"] for i in wasapi_inputs if i["name"] == default_name), None)
            if match is None:
                # Fall back to the physical microphone rather than CABLE Output.
                match = next((i["label"] for i in wasapi_inputs
                              if "cable" not in i["name"].lower()), None)
            if match is None:
                match = next((i["label"] for i in inputs if i["name"] == default_name), "")
            default_input = match or ""
        except (sd.PortAudioError, TypeError, ValueError, IndexError):
            pass
        if not cable:
            cable = next((o["label"] for o in outputs if o["host"] == "Windows WASAPI"), "")
        return {"inputs": inputs, "outputs": outputs, "cableOutput": cable,
                "inputDefault": default_input,
                "virtual": virtual_device.detect(devices=raw_devices, hosts=hosts)}

    def _drain_events(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "status" and payload:
                self.messages.append(str(payload))
            elif kind == "error":
                self.messages.append("错误：" + str(payload))
            if len(self.messages) > 6:
                del self.messages[:-6]
            if kind == "stopped":
                self.running = False

    def _plugins(self) -> list[dict]:
        if not self.chain:
            return []
        items = []
        for index in range(self.chain.count()):
            try:
                info = self.chain.info(index)
            except Vst3Error:
                continue
            info["index"] = index
            info["paramCount"] = self.chain.param_count(index)
            items.append(info)
        return items

    def _status(self) -> dict:
        self._drain_events()
        eng = self.engine
        return {"running": bool(eng and eng.thread and eng.thread.is_alive()),
                "loading": bool(eng and eng.loading),
                "aiMode": eng.ai_mode if eng else self.settings.get("aiMode", "off"),
                "backend": eng.backend if eng else "",
                "rtf": round(eng.rtf, 4) if eng else 0.0,
                "inputDb": round(eng.input_db, 1) if eng else -90.0,
                "outputDb": round(eng.output_db, 1) if eng else -90.0,
                "drops": eng.drops if eng else 0,
                "deviceErrors": eng.devicErrors if eng else 0,
                "underruns": eng.buffer.underruns if eng and eng.buffer else 0,
                "trims": eng.buffer.trims if eng and eng.buffer else 0,
                "muted": eng.options.muted if eng else False,
                "latencyMs": round((eng.latency_samples if eng else 0) * 1000 / CHAIN_RATE, 1),
                "fault": eng.fault_text if eng else "",
                "plugins": self._plugins(),
                "messages": self.messages[-4:],
                "inputRate": eng.input_rate if eng else 0,
                "outputRate": eng.output_rate if eng else CHAIN_RATE,
                "system": self.monitor.metrics(),
                "dsp": eng.dsp.metrics() if eng else {},
                "workshop": {**(eng.workshop.metrics() if eng else {}),
                             "error": getattr(eng, "workshop_error", "") if eng else ""},
                "progress": self.progress,
                "inputSource": self.settings.get("inputSource", "device"),
                "mobileMic": self.mic_server.status(),
                "spectrum": getattr(eng, "spectrum", []) if eng else [],
                "chainError": self.chain_error}

    def _ensure_chain(self):
        if not self.chain:
            raise RuntimeError(self.chain_error or "原生 VST3 宿主不可用")

    # -- dispatch ----------------------------------------------------------
    def handle(self, request: dict) -> dict:
        cmd = request.get("cmd")
        fn = getattr(self, "cmd_" + str(cmd), None)
        if not fn:
            return {"id": request.get("id"), "ok": False, "error": f"未知命令：{cmd}"}
        try:
            result = fn(request)
        except Exception as exc:
            LOG.exception("命令失败：%s", cmd)
            return {"id": request.get("id"), "ok": False, "error": str(exc)}
        return {"id": request.get("id"), "ok": True, "result": result}

    def cmd_hello(self, request):
        devices = self._devices()
        plugins = self._plugins()
        gpu_ok, gpu_note = gpu_capability()
        return {"version": 1, "protocol": PROTOCOL_VERSION, "presetVersion": PRESET_VERSION,
                "devices": devices, "settings": self.settings,
                "plugins": plugins, "running": self.engine.thread.is_alive() if self.engine and self.engine.thread else False,
                "chainError": self.chain_error, "messages": self.messages[-4:],
                "gpuAvailable": gpu_ok, "gpuName": gpu_note,
                "status": self._status()}

    def cmd_devices(self, request):
        return self._devices()

    def cmd_virtual_status(self, request):
        return virtual_device.status()

    def cmd_virtual_open_installer(self, request):
        return virtual_device.open_installer()

    def cmd_virtual_open_download(self, request):
        return virtual_device.open_download()

    def cmd_status(self, request):
        return self._status()

    def _start_config(self, request, settings: dict) -> dict:
        source = str(request.get("inputSource", settings.get("inputSource", "device")))
        cfg = {"inputSource": source,
               "inputDevice": int(request.get("inputDevice", -1)),
               "outputDevice": int(request.get("outputDevice", -1)),
               "aiMode": str(request.get("aiMode", settings.get("aiMode", "off"))),
               "denoise": bool(request.get("denoise", settings.get("denoise", False))),
               "gainDb": float(request.get("gainDb", settings.get("gainDb", 0.0))),
               "floorDb": float(request.get("floorDb", settings.get("floorDb", -55.0))),
               "srStrength": float(request.get("srStrength", settings.get("srStrength", 0.7))),
               "latencyMode": str(request.get("latencyMode", settings.get("latencyMode", "balanced"))),
               "dsp": dsp_merge_params(dsp_default_params(),
                                        request.get("dsp") or settings.get("dsp") or {})}
        if cfg["aiMode"] not in ("off", "cpu", "gpu"):
            raise RuntimeError("AI 模式无效")
        if cfg["aiMode"] == "gpu":
            gpu_ok, gpu_note = gpu_capability()
            if not gpu_ok:
                raise RuntimeError(
                    f"GPU 档不可用（{gpu_note}）。可切换到 CPU 档，或运行 "
                    "tools\\install_gpu_support.bat 安装 CUDA 版 PyTorch 后重试。")
        if source == "mobile":
            if self.mic_server.httpd is None:
                raise RuntimeError("手机麦克风服务未启动。请到「手机麦克风」页启动服务，或用物理麦克风。")
            cfg["inputDevice"] = -1
        elif cfg["inputDevice"] < 0:
            raise RuntimeError("尚未选择输入设备")
        if cfg["aiMode"] != "off":
            from live_voice import PROFILE
            if not PROFILE.is_file():
                raise RuntimeError("尚未录入指定人声纹。请先用旧界面录入或放入 data/target.wav。")
        if cfg["outputDevice"] < 0:
            raise RuntimeError("尚未选择输出设备")
        device = sd.query_devices(cfg["outputDevice"])
        if not any(k in device["name"].lower() for k in ("cable input", "vb-audio virtual cable")):
            LOG.warning("输出设备不是 CABLE Input：%s", device["name"])
        return cfg

    def cmd_start(self, request):
        self._ensure_chain()
        if self.engine.thread and self.engine.thread.is_alive():
            raise RuntimeError("正在运行；请先停止再修改设备")
        cfg = self._start_config(request, self.settings)
        for key in ("inputDevice", "outputDevice", "aiMode", "denoise", "gainDb",
                    "floorDb", "srStrength", "latencyMode", "inputSource"):
            self.settings[key] = cfg[key]
        self.settings["input"] = ("手机麦克风（Wi-Fi）" if cfg["inputSource"] == "mobile"
                                  else device_label(cfg["inputDevice"]))
        self.settings["output"] = device_label(cfg["outputDevice"])
        self.settings["dsp"] = cfg["dsp"]
        self._save_settings()
        self._apply_workshop()
        self.engine.start(cfg)
        self.running = True
        return {"started": True, "status": self._status()}

    def cmd_stop(self, request):
        if self.engine:
            self.engine.stop()
        self.running = False
        return {"stopped": True}

    def cmd_set_options(self, request):
        o = self.engine.options
        if "muted" in request:
            o.muted = bool(request["muted"])
        if "gainDb" in request:
            o.gain_db = max(-24.0, min(24.0, float(request["gainDb"])))
            self.settings["gainDb"] = o.gain_db
        if "denoise" in request:
            o.denoise = bool(request["denoise"])
            self.settings["denoise"] = o.denoise
        if "floorDb" in request:
            o.floor_db = max(-80.0, min(-20.0, float(request["floorDb"])))
            self.settings["floorDb"] = o.floor_db
        if "srStrength" in request:
            o.sr_strength = max(0.0, min(1.0, float(request["srStrength"])))
            self.settings["srStrength"] = o.sr_strength
        if "latencyMode" in request:
            o.latency_mode = str(request["latencyMode"])
            self.settings["latencyMode"] = o.latency_mode
        if "inputSource" in request:
            source = str(request["inputSource"])
            if source not in ("device", "mobile"):
                raise RuntimeError("输入源无效")
            self.settings["inputSource"] = source
            self._save_settings()
        return {"ok": True}

    def cmd_dsp(self, request):
        """读取或更新内置效果器参数。params 缺省 = 只读；带 params = 合并并实时应用。"""
        if request.get("params") is not None:
            # 以当前设置为基底合并：UI 只发变化的字段时，其余参数保持不变。
            base = self.settings.get("dsp") or dsp_default_params()
            merged = dsp_merge_params(base, request["params"])
            self.settings["dsp"] = merged
            if self.engine:
                self.engine.dsp_params = merged
                self.engine.dsp.set_params(merged)
            self._save_settings()
        return {"dsp": self.settings.get("dsp") or dsp_default_params(),
                "metrics": self.engine.dsp.metrics() if self.engine else {}}

    def cmd_scan(self, request):
        dirs = request.get("dirs") or standard_vst3_dirs()
        dirs = [str(Path(d)) for d in dirs if Path(d).is_dir()]
        if not dirs:
            raise RuntimeError("没有可扫描的 VST3 目录")
        result = scan_plugins(dirs)
        self.scan_cache = result["plugins"]
        return result

    def cmd_plugin_add(self, request):
        self._ensure_chain()
        if self.engine.thread and self.engine.thread.is_alive():
            raise RuntimeError("运行中不能修改插件链；请先停止")
        index = self.chain.add(str(request["path"]), str(request["classId"]))
        return {"index": index, "plugins": self._plugins()}

    def cmd_plugin_remove(self, request):
        self._ensure_chain()
        if self.engine.thread and self.engine.thread.is_alive():
            raise RuntimeError("运行中不能修改插件链；请先停止")
        self.chain.remove(int(request["index"]))
        return {"plugins": self._plugins()}

    def cmd_plugin_move(self, request):
        self._ensure_chain()
        if self.engine.thread and self.engine.thread.is_alive():
            raise RuntimeError("运行中不能修改插件链；请先停止")
        self.chain.move(int(request["from"]), int(request["to"]))
        return {"plugins": self._plugins()}

    def cmd_plugin_bypass(self, request):
        self._ensure_chain()
        self.chain.set_bypass(int(request["index"]), bool(request.get("bypass", False)))
        return {"plugins": self._plugins()}

    def cmd_plugin_params(self, request):
        self._ensure_chain()
        return {"params": self.chain.params(int(request["index"]))}

    def cmd_plugin_param(self, request):
        self._ensure_chain()
        self.chain.set_param(int(request["index"]), int(request["param"]), float(request["value"]))
        return {"ok": True}

    def cmd_plugin_open_editor(self, request):
        self._ensure_chain()
        hwnd = self.chain.open_editor(int(request["index"]))
        return {"hwnd": hwnd, "plugins": self._plugins()}

    def cmd_plugin_close_editor(self, request):
        self._ensure_chain()
        self.chain.close_editor(int(request["index"]))
        return {"plugins": self._plugins()}

    # -- 插件市场 -----------------------------------------------------------
    def cmd_market(self, request):
        catalog = market_service.fetch_catalog(ROOT, request.get("url"), bool(request.get("force")))
        installed = market_service.installed_plugins(ROOT)
        known = {item["slug"] for item in installed}
        for plugin in workshop_service.list_plugins(ROOT):
            if plugin["slug"] not in known:
                installed.append({"slug": plugin["slug"], "name": plugin["name"],
                                  "version": plugin["version"], "local": True,
                                  "path": str(workshop_service.plugin_dir(ROOT, plugin["slug"]))})
        return {"catalog": catalog, "installed": installed,
                "userDir": str(market_service.user_vst3_dir())}

    def _catalog_entry(self, slug: str) -> dict:
        catalog = market_service.fetch_catalog(ROOT)
        for entry in catalog.get("plugins") or []:
            if entry["slug"] == slug:
                return entry
        raise RuntimeError("目录中没有该插件，请先刷新目录")

    def cmd_market_install(self, request):
        slug = str(request.get("slug") or "")
        entry = self._catalog_entry(slug)

        def report(done, total):
            self.progress = {"task": "install", "slug": slug, "done": done, "total": total}

        self.progress = {"task": "install", "slug": slug, "done": 0, "total": 0}
        try:
            result = market_service.install(ROOT, entry, progress=report)
        finally:
            self.progress = None
        self.scan_cache = []
        return {"installed": market_service.installed_plugins(ROOT), "path": result["path"]}

    def cmd_market_uninstall(self, request):
        slug = str(request.get("slug") or "")
        market_service.uninstall(ROOT, slug)
        self.scan_cache = []
        return {"installed": market_service.installed_plugins(ROOT)}

    def cmd_market_open(self, request):
        slug = str(request.get("slug") or "")
        entry = self._catalog_entry(slug)
        target = entry.get("homepage") or entry.get("download")
        if not target:
            raise RuntimeError("该插件没有官方页面")
        os.startfile(target)  # noqa: S606 - 只打开用户可见的官方链接
        return {"opened": target}

    # -- 创意工坊 -----------------------------------------------------------
    def _workshop_state(self) -> dict:
        state = self.settings.get("workshop")
        if not isinstance(state, dict):
            state = {"order": [], "enabled": {}, "params": {}}
            self.settings["workshop"] = state
        state.setdefault("order", [])
        state.setdefault("enabled", {})
        state.setdefault("params", {})
        return state

    def cmd_workshop_list(self, request):
        state = self._workshop_state()
        items = []
        for plugin in workshop_service.list_plugins(ROOT):
            compiled = workshop_service.compile_manifest(plugin)
            values = workshop_service.normalize_params(
                compiled.params, (state.get("params") or {}).get(plugin["slug"]) or {})
            items.append({
                **plugin,
                "params": compiled.params,
                "values": values,
                "compileError": compiled.error,
                "enabled": bool((state.get("enabled") or {}).get(plugin["slug"], False)),
                "inChain": plugin["slug"] in (state.get("order") or []),
            })
        return {"plugins": items, "order": state.get("order") or [],
                "marketRepo": workshop_service.MARKET_REPO,
                "workshopDir": str(workshop_service.workshop_dir(ROOT))}

    def cmd_workshop_save(self, request):
        manifest = request.get("plugin") or {}
        saved = workshop_service.save_plugin(ROOT, manifest)
        state = self._workshop_state()
        slug = saved["slug"]
        if slug not in state["order"]:
            state["order"].append(slug)
        state["enabled"][slug] = True
        state["params"].setdefault(slug, {})
        self._apply_workshop()
        self._save_settings()
        return {"plugin": saved, "plugins": workshop_service.list_plugins(ROOT),
                "order": state["order"], "enabled": state["enabled"]}

    def cmd_workshop_delete(self, request):
        slug = str(request.get("slug") or "")
        workshop_service.delete_plugin(ROOT, slug)
        state = self._workshop_state()
        state["order"] = [x for x in state["order"] if x != slug]
        state["enabled"].pop(slug, None)
        state["params"].pop(slug, None)
        self._apply_workshop()
        self._save_settings()
        return {"removed": slug}

    def cmd_workshop_enable(self, request):
        slug = str(request.get("slug") or "")
        enabled = bool(request.get("enabled", True))
        state = self._workshop_state()
        if enabled and slug not in state["order"]:
            state["order"].append(slug)
        state["enabled"][slug] = enabled
        self._apply_workshop()
        self._save_settings()
        errors = getattr(self.engine, "workshop_error", "") if self.engine else ""
        return {"slug": slug, "enabled": enabled, "error": errors}

    def cmd_workshop_params(self, request):
        slug = str(request.get("slug") or "")
        values = request.get("params") or {}
        state = self._workshop_state()
        current = dict(state["params"].get(slug) or {})
        current.update({k: float(v) for k, v in values.items() if isinstance(v, (int, float))})
        state["params"][slug] = current
        if self.engine:
            self.engine.workshop.update_params(slug, current)
        self._save_settings()
        return {"slug": slug, "params": current}

    def cmd_workshop_test(self, request):
        manifest = request.get("plugin") or {}
        return workshop_service.test_manifest(manifest)

    def cmd_workshop_export(self, request):
        slug = str(request.get("slug") or "")
        target = workshop_service.export_zip(ROOT, slug)
        return {"file": str(target)}

    def cmd_workshop_publish(self, request):
        slug = str(request.get("slug") or "")
        result = workshop_service.publish(ROOT, slug, str(request.get("message") or ""))
        return result

    # -- 手机麦克风 ---------------------------------------------------------
    def cmd_mic_start(self, request):
        if request.get("port"):
            self.mic_server._requested = int(request["port"])
        status = self.mic_server.start()
        if status["running"]:
            self.settings["mobileMic"]["port"] = status["port"]
            self._save_settings()
            status["qrInstall"] = self.mic_server.qr_png(status["installUrl"])
            status["qrConnect"] = self.mic_server.qr_png(status["deepLink"])
        return status

    def cmd_mic_stop(self, request):
        return self.mic_server.stop()

    def cmd_mic_status(self, request):
        status = self.mic_server.status()
        status["settings"] = dict(self.settings["mobileMic"])
        status["inputSource"] = self.settings.get("inputSource", "device")
        if request.get("qr") and status["running"]:
            status["qrConnect"] = self.mic_server.qr_png(status["deepLink"])
            status["qrInstall"] = self.mic_server.qr_png(status["installUrl"])
        return status

    def cmd_mic_settings(self, request):
        values = {}
        for key, limit in (("gainDb", 24.0), ("hpfHz", 300.0),
                           ("gateDb", -20.0), ("jitterMs", 300.0)):
            if key in request:
                values[key] = float(request[key])
        self.mobile.configure(**values)
        cfg = self.settings["mobileMic"]
        for key, attr in (("gainDb", "gain_db"), ("hpfHz", "hpf_hz"),
                          ("gateDb", "gate_db"), ("jitterMs", "jitter_ms")):
            cfg[key] = getattr(self.mobile, attr)
        self._save_settings()
        return {"mic": self.mobile.metrics(), "settings": cfg}

    def cmd_mic_open_apk(self, request):
        apk = find_mobile_apk()
        install_url = (self.mic_server.status()["installUrl"]
                       if self.mic_server.httpd else "")
        if str(request.get("mode") or "") == "page":
            if not install_url:
                raise RuntimeError("请先启动手机麦克风服务")
            os.startfile(install_url)  # noqa: S606
            return {"opened": install_url}
        if apk is not None:
            subprocess.Popen(["explorer", "/select,", str(apk)])
            return {"opened": str(apk)}
        if install_url:
            os.startfile(install_url)  # noqa: S606
            return {"opened": install_url}
        raise RuntimeError("还没有 APK，且手机麦克风服务未启动")

    # -- 自动更新 -----------------------------------------------------------
    def cmd_update_check(self, request):
        return market_service.check_update(ROOT)

    def cmd_update_download(self, request):
        info = market_service.check_update(ROOT)
        if not info.get("ok") or not info.get("available"):
            raise RuntimeError(info.get("error") or "当前已是最新版本")
        assets = info.get("assets") or []
        wanted = str(request.get("asset") or "")
        asset = next((a for a in assets if a["name"] == wanted), None)
        if asset is None:
            asset = next((a for a in assets if str(a["name"]).lower().endswith(".zip")), None)
        if asset is None:
            raise RuntimeError("该版本没有可下载的 zip 包")

        def report(done, total):
            self.progress = {"task": "update", "slug": "update", "done": done, "total": total}

        self.progress = {"task": "update", "slug": "update", "done": 0, "total": 0}
        try:
            got = market_service.download_update(ROOT, asset["url"],
                                                 str(request.get("sha256") or ""), progress=report)
        finally:
            self.progress = None
        return {"download": got, "version": info["latest"]}

    def cmd_update_apply(self, request):
        package = Path(str(request.get("file") or ""))
        if not package.is_file():
            raise RuntimeError("更新包不存在，请先下载")
        script = market_service.write_apply_script(ROOT, package)
        subprocess.Popen(["cmd", "/c", "start", "", str(script)],
                         cwd=str(ROOT), creationflags=subprocess.CREATE_NEW_CONSOLE)
        self.should_stop = True
        return {"applying": True, "script": str(script)}

    @staticmethod
    def _preset_summary(payload: dict) -> dict:
        """给界面用的预设内容摘要：让用户在载入前知道这套预设都包含什么。"""
        cfg = payload.get("config") or {}
        dsp = cfg.get("dsp") or {}
        effects = []
        if (dsp.get("hpf") or {}).get("enabled") or (dsp.get("lpf") or {}).get("enabled"):
            effects.append("滤波")
        if (dsp.get("eq") or {}).get("enabled"):
            effects.append("EQ")
        if (dsp.get("comp") or {}).get("enabled"):
            effects.append("压缩")
        if (dsp.get("gate") or {}).get("enabled"):
            effects.append("门限")
        if dsp and not dsp.get("enabled"):
            effects = []
        mode = str(cfg.get("aiMode") or "off")
        return {
            "plugins": len(payload.get("plugins") or []),
            "aiMode": mode,
            "aiLabel": {"off": "AI 关", "cpu": "AI CPU", "gpu": "AI GPU"}.get(mode, mode),
            "dspEnabled": bool(dsp.get("enabled")),
            "effects": effects,
            "workshop": len((cfg.get("workshop") or {}).get("order") or []),
        }

    def cmd_preset_list(self, request):
        items = []
        for path in sorted(PRESET_DIR.glob("*.json"), key=lambda p: p.stem.lower()):
            saved = ""
            summary = {"plugins": 0, "aiMode": "off", "aiLabel": "AI 关",
                       "dspEnabled": False, "effects": [], "workshop": 0}
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                saved = str(payload.get("savedAt") or "")
                summary = self._preset_summary(payload)
            except (OSError, ValueError, TypeError):
                payload = {}
            if not saved:
                saved = time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
            items.append({"name": path.stem, "savedAt": saved, **summary})
        return {"presets": items, "names": [i["name"] for i in items]}

    def cmd_preset_save(self, request):
        self._ensure_chain()
        name = str(request.get("name", "")).strip()
        if not name or any(c in name for c in '\\/:*?"<>|'):
            raise RuntimeError("预设名称无效")
        plugins = []
        for index in range(self.chain.count()):
            info = self.chain.info(index)
            state = base64.b64encode(self.chain.get_state(index)).decode("ascii")
            params = {str(p["id"]): p["value"] for p in self.chain.params(index)
                      if not p.get("isReadOnly")}
            plugins.append({"path": info["path"], "classId": info["classId"], "name": info["name"],
                            "bypass": info["bypass"], "params": params, "state": state})
        payload = {"version": PRESET_VERSION, "name": name, "savedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "config": dict(self.settings), "plugins": plugins}
        target = PRESET_DIR / (name + ".json")
        temp = target.with_suffix(".pending")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, target)
        return {"presets": sorted(p.stem for p in PRESET_DIR.glob("*.json"))}

    def cmd_preset_load(self, request):
        self._ensure_chain()
        if self.engine.thread and self.engine.thread.is_alive():
            raise RuntimeError("加载预设前请先停止处理")
        name = str(request.get("name", ""))
        path = PRESET_DIR / (name + ".json")
        if not path.is_file():
            raise RuntimeError("预设不存在")
        payload = json.loads(path.read_text(encoding="utf-8"))

        version = int(payload.get("version", 0) or 0)
        if version > PRESET_VERSION:
            raise RuntimeError(
                f"预设版本 {version} 高于当前程序支持的 {PRESET_VERSION}，请升级程序后再载入")

        # 预检：插件缺失时直接报错，绝不先清空当前链，保证「失败保留可用链路」。
        items = payload.get("plugins") or []
        missing = [str(item.get("name") or item.get("path") or "未知插件")
                   for item in items if not Path(str(item.get("path", ""))).is_file()]
        if missing:
            raise RuntimeError("预设中的插件缺失，未改动当前链路：" + "、".join(missing[:5])
                               + ("等" if len(missing) > 5 else ""))

        # 预检通过后才动链。逐项失败也回滚到载入前的链。
        previous = [(info["path"], info["classId"]) for info in
                    (self.chain.info(i) for i in range(self.chain.count()))]
        try:
            while self.chain.count():
                self.chain.remove(0)
            for item in items:
                index = self.chain.add(item["path"], item["classId"])
                if item.get("state"):
                    self.chain.set_state(index, base64.b64decode(item["state"]))
                for pid, value in (item.get("params") or {}).items():
                    params = self.chain.params(index)
                    pos = next((p["index"] for p in params if str(p["id"]) == str(pid)), None)
                    if pos is not None:
                        self.chain.set_param(index, pos, float(value))
                self.chain.set_bypass(index, bool(item.get("bypass", False)))
        except Exception as exc:
            LOG.exception("载入预设失败，回滚插件链")
            try:
                while self.chain.count():
                    self.chain.remove(0)
                for plugin_path, class_id in previous:
                    self.chain.add(plugin_path, class_id)
            except Exception:
                LOG.exception("回滚插件链也失败")
            raise RuntimeError(f"载入预设失败，已回滚到载入前的插件链：{exc}") from exc

        cfg = payload.get("config") or {}
        for key in ("aiMode", "denoise", "gainDb", "floorDb", "srStrength", "latencyMode"):
            if key in cfg:
                self.settings[key] = cfg[key]
        if "dsp" in cfg:
            # 预设里的 DSP 参数按版本合并恢复，并立即下发到音频线程。
            self.settings["dsp"] = dsp_merge_params(dsp_default_params(), cfg["dsp"])
            if self.engine:
                self.engine.dsp_params = self.settings["dsp"]
                self.engine.dsp.set_params(self.settings["dsp"])
        if "workshop" in cfg:
            shop = cfg.get("workshop") or {}
            self.settings["workshop"] = {
                "order": [x for x in (shop.get("order") or []) if isinstance(x, str)],
                "enabled": dict(shop.get("enabled") or {}),
                "params": dict(shop.get("params") or {}),
            }
        # 工坊阶段按新设置重新编译并热替换。
        self._apply_workshop()
        self._save_settings()
        return {"plugins": self._plugins(), "settings": self.settings}

    def cmd_preset_delete(self, request):
        name = str(request.get("name", ""))
        path = PRESET_DIR / (name + ".json")
        if path.is_file():
            path.unlink()
        return {"presets": sorted(p.stem for p in PRESET_DIR.glob("*.json"))}

    def cmd_shutdown(self, request):
        self._save_settings()
        self.should_stop = True
        return {"shutdown": True}

    def close(self):
        if self.engine:
            self.engine.stop()
            if self.engine.thread:
                self.engine.thread.join(timeout=8)
        if self.chain:
            self.chain.close()


def main() -> None:
    MIXER_DATA.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=MIXER_DATA / "service.log", encoding="utf-8", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    LOG.info("mixer_service starting")
    service = Service()
    server = PipeServer(PIPE_NAME)
    try:
        while not service.should_stop:
            if not server.accept():
                time.sleep(0.5)
                continue
            LOG.info("UI connected")
            buffer = bytearray()
            try:
                while not service.should_stop:
                    lines = server.read_line_parts(buffer)
                    if lines is None:
                        break
                    for line in lines:
                        try:
                            request = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        response = service.handle(request)
                        if not server.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n"):
                            break
                        if service.should_stop:
                            # 给客户端留出读完 shutdown 响应的时间，否则关句柄会丢掉已写数据。
                            time.sleep(0.1)
                            break
            finally:
                server.disconnect()
                LOG.info("UI disconnected")
    except KeyboardInterrupt:
        pass
    finally:
        service.close()
        server.disconnect()
        LOG.info("mixer_service stopped")


if __name__ == "__main__":
    # PyInstaller 单文件模式下，插件扫描子进程复用本 exe：--scan-worker 分支。
    if "--scan-worker" in sys.argv:
        import vst3_scan_worker
        sys.exit(vst3_scan_worker.main())
    main()
