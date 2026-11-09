from __future__ import annotations

import argparse
from collections import deque
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import ctypes
import json
import logging
import os
from pathlib import Path
import queue
import shutil
import threading
import time
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except Exception:  # the packaged mixer engine runs headless, without Tcl/Tk
    tk = ttk = filedialog = messagebox = None

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import dpdfnet
import numpy as np
import sounddevice as sd
import soundfile as sf
import soxr
import voxlock
from obs_audio import OBS_DEVICE, OBS_LABEL, OBS_URL

from app_paths import ROOT, DATA

PROFILE = DATA / "target.wav"
SETTINGS = DATA / "settings.json"
SR = 16000
BLOCK_MS = 40
LOG = logging.getLogger("live_voice")


def replace_file(temp: Path, destination: Path) -> None:
    if destination.exists():
        shutil.copy2(destination, destination.with_suffix(destination.suffix + ".bak"))
    os.replace(temp, destination)


def validate_sample(audio: np.ndarray) -> np.ndarray:
    x = np.asarray(audio, dtype=np.float32).reshape(-1)
    if len(x) < SR * 3 or not np.isfinite(x).all():
        raise ValueError("需要至少 3 秒有效录音，建议单独连续说话 6 秒。")
    frames = x[:len(x) // 320 * 320].reshape(-1, 320)
    energy = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-12)
    threshold = max(0.003, float(energy.max()) * 0.08)
    if np.count_nonzero(energy > threshold) * 0.02 < 3:
        raise ValueError("有效声音不足 3 秒，请靠近麦克风、在安静环境中重新录入。")
    if np.mean(np.abs(x) >= 0.995) > 0.01:
        raise ValueError("录音存在明显削波，请降低麦克风增益后重新录入。")
    return x


def save_sample(audio: np.ndarray) -> None:
    audio = validate_sample(audio)
    DATA.mkdir(parents=True, exist_ok=True)
    temp = DATA / "target.pending.wav"
    sf.write(temp, audio, SR, subtype="FLOAT")
    replace_file(temp, PROFILE)


@contextmanager
def windows_audio_context():
    # WASAPI marshals COM interfaces in Pa_StartStream on this caller thread.
    # PortAudio's own callback thread initialization does not cover our worker.
    if os.name != "nt":
        yield
        return
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    result = ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
    if result not in (0, 1, -2147417850):  # S_OK, S_FALSE, RPC_E_CHANGED_MODE
        raise OSError(f"Windows 音频线程初始化失败：0x{result & 0xffffffff:08X}")
    try:
        yield
    finally:
        if result in (0, 1):
            ole32.CoUninitialize()


def device_options(device: int):
    info = sd.query_devices(device)
    host = sd.query_hostapis(info["hostapi"])["name"]
    if host == "Windows WASAPI":
        return sd.WasapiSettings(exclusive=False, auto_convert=True)
    return None


@contextmanager
def open_audio_stream(direction: str, *, notify=None, **kwargs):
    # Close partially opened streams too: __enter__ does not call __exit__
    # when Pa_StartStream fails, which otherwise leaves the device allocated.
    factory = sd.InputStream if direction == "input" else sd.OutputStream
    kwargs.setdefault("extra_settings", device_options(kwargs["device"]))
    stream = None
    for latency in ("low", "high"):
        try:
            stream = factory(latency=latency, **kwargs)
            stream.start()
        except sd.PortAudioError:
            if stream is not None:
                stream.close()
                stream = None
            if latency == "high":
                raise
            if notify:
                name = "输入" if direction == "input" else "输出"
                notify(f"{name}设备低延迟模式未能启动，正在使用兼容缓冲重试…")
        else:
            break
    try:
        yield stream
    finally:
        try:
            stream.stop()
        finally:
            stream.close()


def device_format(device: int, direction: str) -> tuple[int, int]:
    info = sd.query_devices(device)
    maximum = int(info[f"max_{direction}_channels"])
    check = sd.check_input_settings if direction == "input" else sd.check_output_settings
    # Prefer the endpoint's native rate, especially for WASAPI shared mode.
    native = int(info["default_samplerate"])
    rates = dict.fromkeys((48000, native, 44100, SR) if "cable" in info["name"].lower()
                          else (native, 48000, 44100, SR))
    channels = (1, 2) if direction == "input" else (2, 1)
    for rate in rates:
        for count in channels:
            if count > maximum:
                continue
            try:
                check(device=device, samplerate=rate, channels=count, dtype="float32",
                      extra_settings=device_options(device))
                return rate, count
            except sd.PortAudioError:
                pass
    raise RuntimeError(f"设备不支持所需音频格式：{info['name']}")


class Resampler:
    def __init__(self, source: int, target: int):
        self.stream = (soxr.ResampleStream(source, target, 1, dtype="float32", quality="MQ")
                       if source != target else None)

    def process(self, audio: np.ndarray) -> np.ndarray:
        if not len(audio):
            return np.empty(0, np.float32)
        return self.stream.resample_chunk(audio) if self.stream else audio


class AudioBuffer:
    """Bounded output FIFO; only processed samples can enter it."""

    def __init__(self, rate: int, prefill_ms: int = 80):
        self.lock = threading.Lock()
        self.blocks: deque[np.ndarray] = deque()
        self.offset = 0
        self.size = 0
        self.maximum = round(rate * 0.20)
        self.prefill = round(rate * prefill_ms / 1000)
        self.ready = False
        self.started = False
        self.underruns = 0
        self.trims = 0

    def put(self, audio: np.ndarray) -> None:
        if not len(audio):
            return
        with self.lock:
            self.blocks.append(np.asarray(audio, dtype=np.float32))
            self.size += len(audio)
            # Independent capture/playback clocks may drift. Drop only processed
            # samples to bound latency instead of replaying stale microphone audio.
            if self.size > self.maximum:
                self.trims += 1
                while self.size > self.prefill and len(self.blocks) > 1:
                    self.size -= len(self.blocks.popleft()) - self.offset
                    self.offset = 0

    def fill(self, outdata: np.ndarray) -> None:
        outdata.fill(0)
        with self.lock:
            if not self.ready:
                if self.size < self.prefill:
                    return
                self.ready = self.started = True
            written = 0
            frames = len(outdata)
            while self.blocks and written < frames:
                block = self.blocks[0]
                count = min(frames - written, len(block) - self.offset)
                outdata[written:written + count] = block[self.offset:self.offset + count, None]
                self.offset += count
                self.size -= count
                written += count
                if self.offset == len(block):
                    self.blocks.popleft()
                    self.offset = 0
            if written < frames:
                self.underruns += 1
                self.ready = False

    def clear(self) -> None:
        with self.lock:
            self.blocks.clear()
            self.size = self.offset = 0
            self.ready = False


class PersonalExtractor:
    def __init__(self, path: Path):
        self.processor = dpdfnet.StreamEnhancer(model="dpdfnet2_48khz_hr", onnx_path=path)

    def stream(self, sample=None):
        self.processor.reset()
        return self

    def push(self, audio):
        return self.processor.process(audio, sample_rate=48000)


class Models:
    def __init__(self, personal_model: Path | None = None, superres: bool = False,
                 gpu: bool = False, latency_mode: str = "balanced"):
        self.extractor = None
        self.denoiser = None
        self.personal_model = personal_model
        self.sample_rate = 48000 if personal_model else SR
        self.gpu = gpu
        self.latency_mode = latency_mode
        self.extractor_backend = "CPU"
        self.superres = superres or gpu
        self.superres_session = None
        self.gpu_backend = None
        self.backend_name = "CPU · FlashSR" if self.superres else "CPU"

    def load(self, notify) -> None:
        if self.personal_model is not None:
            if self.extractor is None:
                if not self.personal_model.is_file():
                    raise FileNotFoundError("尚未导出个人模型，请先运行 start_personal.bat。")
                notify("加载个人 48 kHz 模型…")
                self.extractor = PersonalExtractor(self.personal_model)
            return
        if self.extractor is None:
            notify("加载声纹提取模型，首次使用需要下载…")
            if self.gpu:
                try:
                    from gpu_extractor import CudaVoxLock
                    self.extractor = CudaVoxLock()
                    self.extractor_backend = "声纹 CUDA FP32"
                except Exception:
                    LOG.warning("CUDA 声纹提取不可用，使用 CPU FP32", exc_info=True)
                    notify("声纹 CUDA 不可用，使用 CPU FP32；超分仍使用 GPU。")
            if self.extractor is None:
                self.extractor = voxlock.VoxLock(precision="fp32" if self.gpu else "int8", threads=2)
                self.extractor_backend = "声纹 CPU FP32" if self.gpu else "声纹 CPU INT8"
        if self.denoiser is None:
            notify("加载实时降噪模型…")
            self.denoiser = dpdfnet.StreamEnhancer(model="dpdfnet2")
        if self.gpu and self.gpu_backend is None:
            notify("加载 CUDA · LavaSR v2 高质量超分，预热 GPU…")
            from gpu_super_resolution import prepare_gpu_model
            import torch
            self.gpu_backend = prepare_gpu_model(self.latency_mode)
            self.backend_name = f"{self.extractor_backend} · {torch.cuda.get_device_name(0)} · LavaSR v2"
        elif self.superres and not self.gpu and self.superres_session is None:
            notify("加载 16→48 kHz 人声超分模型…")
            from super_resolution import prepare_model
            self.superres_session = prepare_model()
        if self.gpu_backend is not None:
            from gpu_super_resolution import LATENCY_PROFILES
            params = LATENCY_PROFILES[self.latency_mode]
            self.gpu_backend.prepare((5632 + params["hop"] + params["lookahead"]) * 3)


class Pipeline:
    def __init__(self, models: Models, sample: np.ndarray, input_rate: int, output_rate: int):
        self.extract = models.extractor.stream(sample)
        self.denoise = models.denoiser
        self.rate = models.sample_rate
        if self.denoise is not None:
            self.denoise.reset()
        self.input_resampler = Resampler(input_rate, self.rate)
        self.super_resolution = None
        if models.gpu_backend is not None:
            from gpu_super_resolution import StreamingLavaSR
            self.super_resolution = StreamingLavaSR(models.gpu_backend, models.latency_mode)
        elif models.superres_session is not None:
            from super_resolution import StreamingSuperResolution
            self.super_resolution = StreamingSuperResolution(models.superres_session)
        self.post_rate = 48000 if self.super_resolution else self.rate
        self.output_resampler = Resampler(self.post_rate, output_rate)
        self.gate = 0.0
        self.limiter_gain = 1.0

    def process(self, audio: np.ndarray, denoise: bool, floor_db: float, gain_db: float, sr_strength: float = .7) -> np.ndarray:
        x = self.input_resampler.process(audio)
        if not len(x):
            return np.empty(0, np.float32)
        y = self.extract.push(x)
        if not len(y):
            return np.empty(0, np.float32)
        if denoise and self.denoise is not None:
            y = self.denoise.process(y, sample_rate=self.rate)
        if not len(y):
            return np.empty(0, np.float32)
        if not np.isfinite(y).all():
            raise RuntimeError("模型输出异常，已停止并静音。")
        level = float(np.sqrt(np.mean(y ** 2) + 1e-12))
        target = float(level >= 10 ** (floor_db / 20))
        # Fast attack, slow release; never mix the unfiltered microphone back in.
        tau = 0.005 if target > self.gate else 0.08
        envelope = target + (self.gate - target) * np.exp(
            -np.arange(1, len(y) + 1, dtype=np.float32) / (self.rate * tau))
        y = y * envelope
        self.gate = float(envelope[-1])
        y *= 10 ** (gain_db / 20)
        if self.super_resolution is not None:
            y = self.super_resolution.push(y, sr_strength)
            if not len(y):
                return np.empty(0, np.float32)
        # A sample-wise peak limiter with slow recovery avoids changing the
        # gain of an entire 40 ms block for one transient consonant.
        if self.limiter_gain < 0.9999 or float(np.max(np.abs(y))) > 0.98:
            required = np.minimum(1.0, 0.98 / np.maximum(np.abs(y), 1e-9))
            # d[n] = max(1-required[n], alpha*d[n-1]). Its exponentially
            # weighted running maximum computes the same envelope in NumPy,
            # without thousands of Python iterations per audio block.
            decay = np.exp(-np.arange(1, len(y) + 1, dtype=np.float64) / (self.post_rate * .10))
            deficit = np.maximum.accumulate((1.0 - required.astype(np.float64)) / decay)
            gains = 1.0 - np.maximum(deficit, 1.0 - self.limiter_gain) * decay
            y *= gains.astype(np.float32)
            self.limiter_gain = float(gains[-1])
        return self.output_resampler.process(y.astype(np.float32))


@dataclass
class LiveOptions:
    gain_db: float = 0.0
    floor_db: float = -55.0
    muted: bool = False
    sr_strength: float = .7


class AudioEngine:
    def __init__(self, models: Models, events: queue.Queue):
        self.models = models
        self.events = events
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.options = LiveOptions()
        self.rtf = 0.0
        self.input_db = self.output_db = -90.0
        self.drops = 0
        self.device_errors = 0
        self.buffer: AudioBuffer | None = None
        self.obs_output = None

    def start(self, input_device: int, output_device: int, denoise: bool) -> None:
        self.stop_event.clear()
        self.buffer = None
        self.obs_output = None
        self.thread = threading.Thread(target=self._run,
                                       args=(input_device, output_device, denoise), daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.buffer:
            self.buffer.clear()

    def _run(self, input_device: int, output_device: int, denoise: bool) -> None:
        notify = lambda text: self.events.put(("status", text))
        try:
            with windows_audio_context():
                sample = None if self.models.personal_model else validate_sample(voxlock.load_audio(PROFILE))
                self.models.load(notify)
                if self.stop_event.is_set():
                    return
                input_rate, input_channels = device_format(input_device, "input")
                output_rate, output_channels = ((48000, 1) if output_device == OBS_DEVICE
                                                 else device_format(output_device, "output"))
                if self.models.superres and output_rate < 44100:
                    raise RuntimeError("当前输出设备处于低采样率模式，无法保留超分高频。请选蓝牙 Stereo 立体声、"
                                       "有线耳机或 48 kHz 虚拟声卡，不要选 Hands-Free 通话设备。")
                block_ms, prefill_ms = BLOCK_MS, 80
                if self.models.gpu:
                    from gpu_super_resolution import LATENCY_PROFILES
                    params = LATENCY_PROFILES[self.models.latency_mode]
                    block_ms, prefill_ms = params["block_ms"], params["prefill_ms"]
                self.buffer = AudioBuffer(output_rate, prefill_ms)
                pending: queue.Queue = queue.Queue(maxsize=5)
                pipeline = Pipeline(self.models, sample, input_rate, output_rate)
                self.rtf = self.drops = self.device_errors = 0
                chunk_size = round(input_rate * block_ms / 1000)

                def capture(indata, frames, timing, status):
                    if self.stop_event.is_set():
                        raise sd.CallbackStop
                    if status:
                        self.device_errors += 1
                    packet = (time.monotonic(), indata[:, 0].copy())
                    try:
                        pending.put_nowait(packet)
                    except queue.Full:
                        try:
                            pending.get_nowait()
                        except queue.Empty:
                            pass
                        self.drops += 1
                        pending.put_nowait(packet)

                def playback(outdata, frames, timing, status):
                    if status:
                        self.device_errors += 1
                    if self.stop_event.is_set():
                        outdata.fill(0)
                        raise sd.CallbackStop
                    self.buffer.fill(outdata)
                    if self.options.muted:
                        outdata.fill(0)

                with ExitStack() as stack:
                    if output_device == OBS_DEVICE:
                        from obs_audio import ObsAudioOutput
                        output_stream = stack.enter_context(ObsAudioOutput(playback))
                        self.obs_output = output_stream
                    else:
                        output_stream = stack.enter_context(open_audio_stream("output", notify=notify,
                            device=output_device, samplerate=output_rate, channels=output_channels,
                            dtype="float32", blocksize=round(output_rate * (0.01 if self.models.gpu else 0.02)),
                            callback=playback))
                    input_stream = stack.enter_context(open_audio_stream("input", notify=notify,
                        device=input_device, samplerate=input_rate, channels=input_channels,
                        dtype="float32", blocksize=chunk_size, callback=capture))
                    self.events.put(("running", (input_rate, output_rate)))
                    stalls = 0
                    previous_drops = 0
                    last_audio = time.monotonic()
                    while not self.stop_event.is_set():
                        if not input_stream.active or not output_stream.active:
                            raise RuntimeError(getattr(output_stream, "error", None) or
                                               "音频设备已断开或停止，请重新选择设备。")
                        try:
                            captured_at, audio = pending.get(timeout=0.10)
                        except queue.Empty:
                            if time.monotonic() - last_audio > 3:
                                raise RuntimeError("麦克风连续 3 秒未返回音频，已停止。")
                            continue
                        last_audio = time.monotonic()
                        if self.drops != previous_drops or last_audio - captured_at > 0.24:
                            previous_drops = self.drops
                            stalls += 1
                            self.buffer.clear()
                            if stalls >= 3:
                                raise RuntimeError("处理速度持续不足，已静音。请关闭降噪或减少后台负载后重试。")
                            while not pending.empty():
                                try:
                                    captured_at, audio = pending.get_nowait()
                                except queue.Empty:
                                    break
                            pipeline = Pipeline(self.models, sample, input_rate, output_rate)
                        started = time.perf_counter()
                        opts = self.options
                        processed = pipeline.process(audio, denoise, opts.floor_db, opts.gain_db, opts.sr_strength)
                        elapsed = time.perf_counter() - started
                        self.rtf = 0.9 * self.rtf + 0.1 * elapsed / (len(audio) / input_rate)
                        self.input_db = 20 * np.log10(float(np.sqrt(np.mean(audio ** 2))) + 1e-9)
                        self.output_db = (20 * np.log10(float(np.sqrt(np.mean(processed ** 2))) + 1e-9)
                                          if len(processed) else -90.0)
                        self.buffer.put(processed)
        except Exception as exc:
            LOG.exception("音频处理停止")
            self.events.put(("error", str(exc)))
        finally:
            self.stop_event.set()
            if self.buffer:
                self.buffer.clear()
            self.events.put(("stopped", None))


class App:
    def __init__(self, root: tk.Tk, personal_model: Path | None = None, superres: bool = False,
                 gpu: bool = False, obs_output: bool = False, cable_output: bool = False):
        self.root = root
        self.personal_model = personal_model
        self.gpu = gpu
        self.prefer_cable = cable_output
        self.superres = superres or gpu
        self.root.title("直播麦克风 · 指定人声")
        self.root.geometry("820x740")
        self.root.minsize(760, 710)
        self.events: queue.Queue = queue.Queue()
        self.models = Models(personal_model, superres, gpu)
        self.engine = AudioEngine(self.models, self.events)
        self.busy = False
        self.closing = False
        self.record_stop = threading.Event()
        self.task: threading.Thread | None = None
        self.status = tk.StringVar(value="选择麦克风，先录入或导入指定人的声纹样本。")
        self.profile = tk.StringVar(value="未录入声纹")
        self.stats = tk.StringVar(value="尚未开始")
        self.input_name = tk.StringVar()
        self.output_name = tk.StringVar()
        self.routing_help = tk.StringVar()
        self.denoise = tk.BooleanVar(value=False)
        self.gain = tk.DoubleVar(value=0.0)
        self.floor = tk.DoubleVar(value=-55.0)
        self.sr_strength = tk.DoubleVar(value=70)
        self.latency_mode = tk.StringVar(value="平衡（224 ms 前瞻）")
        self.latency_info = tk.StringVar()
        self.devices = {}
        self.collector = None
        self._build()
        self.refresh_devices()
        self._restore()
        if obs_output:
            self.output_name.set(OBS_LABEL)
        elif cable_output:
            self._select_cable_output()
        self._update_output_help()
        self._update_latency_info()
        self._profile_status()
        self.set_busy(False)
        if self.personal_model:
            self.status.set("个人 48 kHz 模型已选择。先用耳机试听，再连接直播。")
        elif self.gpu:
            self.status.set("已选择 CUDA 人声处理。可选择前瞻档位，切换后重新开始处理。")
        elif self.superres:
            self.status.set("已选择声纹提取 + 48 kHz 超分。先用耳机试听，调节高频补全强度。")
        if self.output_name.get() == OBS_LABEL:
            self.status.set("已选择 OBS 独立人声。点击「开始处理」后，再在 OBS 添加媒体源接收。")
        elif cable_output:
            self.status.set("已选择 CABLE 虚拟声卡，OBS 麦克风请选择 CABLE Output。" if self.output_name.get()
                            else "尚未检测到 CABLE Input。请完成驱动安装，必要时重启电脑，再点击「刷新设备」。")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Control-m>", lambda _event: self.toggle_mute())
        self.root.after(100, self.tick)

    def _build(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=7)
        frame = ttk.Frame(self.root, padding=22)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text="个人 48 kHz 人声过滤 · 试听" if self.personal_model else ("指定人声 + GPU 高质量超分" if self.gpu else ("指定人声 + 48 kHz 超分" if self.superres else "只保留你指定的声音")), font=("Microsoft YaHei UI", 19, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 14))
        ttk.Label(frame, text="麦克风输入").grid(row=1, column=0, sticky="w")
        self.input_box = ttk.Combobox(frame, textvariable=self.input_name, state="readonly")
        self.input_box.grid(row=2, column=0, sticky="ew", pady=(4, 10))
        ttk.Label(frame, text="处理后的音频输出").grid(row=3, column=0, sticky="w")
        self.output_box = ttk.Combobox(frame, textvariable=self.output_name, state="readonly")
        self.output_box.grid(row=4, column=0, sticky="ew", pady=(4, 8))
        self.output_box.bind("<<ComboboxSelected>>", lambda _event: self._update_output_help())
        route_buttons = ttk.Frame(frame)
        route_buttons.grid(row=5, column=0, sticky="w")
        self.refresh_button = ttk.Button(route_buttons, text="刷新设备", command=self.refresh_devices)
        self.refresh_button.pack(side="left")
        self.copy_obs_button = ttk.Button(route_buttons, text="复制 OBS 直连地址", command=self.copy_obs_url)
        ttk.Label(frame, wraplength=700, foreground="#526477",
                  textvariable=self.routing_help).grid(
            row=6, column=0, sticky="ew", pady=(8, 12))
        voice = ttk.LabelFrame(frame, text="指定人的声纹", padding=12)
        voice.grid(row=7, column=0, sticky="ew")
        ttk.Label(voice, textvariable=self.profile).pack(anchor="w")
        buttons = ttk.Frame(voice)
        buttons.pack(fill="x", pady=(8, 0))
        self.record_button = ttk.Button(buttons, text="录入声纹（6 秒）", command=self.record)
        self.record_button.pack(side="left")
        self.import_button = ttk.Button(buttons, text="导入声音样本", command=self.import_sample)
        self.import_button.pack(side="left", padx=8)
        self.collect_button = ttk.Button(buttons, text="采集训练原声（48k）", command=self.open_collector)
        self.collect_button.pack(side="left")
        ttk.Label(voice, text="此模型已针对你的录音训练，无需再录入声纹。" if self.personal_model else "安静环境中由目标人物单独连续说话；样本仅保存在本机。",
                  foreground="#526477").pack(anchor="w", pady=(6, 0))
        if self.superres:
            sr_row = ttk.Frame(voice)
            sr_row.pack(fill="x", pady=(8, 0))
            ttk.Label(sr_row, text="高频补全强度 %").pack(side="left")
            ttk.Spinbox(sr_row, from_=0, to=100, increment=10, width=5, textvariable=self.sr_strength).pack(side="left", padx=8)
            ttk.Label(sr_row, text="0 为关闭补全；高频由模型估计。", foreground="#526477").pack(side="left")
        if self.gpu:
            latency_row = ttk.Frame(voice)
            latency_row.pack(fill="x", pady=(8, 0))
            ttk.Label(latency_row, text="延迟档位").pack(side="left")
            from gpu_super_resolution import LATENCY_PROFILES
            self.latency_box = ttk.Combobox(latency_row, textvariable=self.latency_mode, width=25,
                state="readonly", values=[p["label"] for p in LATENCY_PROFILES.values()])
            self.latency_box.pack(side="left", padx=8)
            self.latency_box.bind("<<ComboboxSelected>>", lambda _event: self._update_latency_info())
        options = ttk.Frame(frame)
        options.grid(row=8, column=0, sticky="ew", pady=12)
        self.denoise_box = ttk.Checkbutton(options, text="额外降噪（自然音色时关闭）", variable=self.denoise)
        self.denoise_box.pack(side="left")
        ttk.Label(options, text="输出增益 dB").pack(side="left", padx=(18, 5))
        ttk.Spinbox(options, from_=-12, to=18, increment=1, width=5, textvariable=self.gain).pack(side="left")
        ttk.Label(options, text="静音门限 dB").pack(side="left", padx=(18, 5))
        ttk.Spinbox(options, from_=-80, to=-25, increment=1, width=5, textvariable=self.floor).pack(side="left")
        controls = ttk.Frame(frame)
        controls.grid(row=9, column=0, sticky="ew")
        self.start_button = ttk.Button(controls, text="开始处理", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="停止", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.mute_button = ttk.Button(controls, text="静音（Ctrl+M）", command=self.toggle_mute, state="disabled")
        self.mute_button.pack(side="left")
        ttk.Label(frame, textvariable=self.status, wraplength=700).grid(
            row=10, column=0, sticky="ew", pady=(16, 5))
        ttk.Label(frame, textvariable=self.stats, foreground="#526477", wraplength=700).grid(
            row=11, column=0, sticky="ew")
        ttk.Label(frame, textvariable=self.latency_info,
                  foreground="#526477", wraplength=700).grid(row=12, column=0, sticky="ew", pady=(12, 0))

    def _latency_key(self):
        if self.gpu:
            from gpu_super_resolution import LATENCY_PROFILES
            return next((key for key, p in LATENCY_PROFILES.items() if p["label"] == self.latency_mode.get()), "balanced")
        return "balanced"

    def _update_latency_info(self):
        if self.gpu:
            from gpu_super_resolution import LATENCY_PROFILES
            params = LATENCY_PROFILES[self._latency_key()]
            self.latency_info.set(f"48 kHz 输出；超分前瞻 {params['lookahead'] / 16:g} ms，音频预缓冲 {params['prefill_ms']} ms，另有声纹处理和声卡延迟。\n"
                                  "短前瞻会改变补出的高频；可切回「完整上下文」。未使用原麦克风混回。")
        else:
            self.latency_info.set("个人模型原生处理 48 kHz；效果需实录试听。" if self.personal_model else
                ("16 kHz 声纹提取 → 高频补全 → 48 kHz 输出；超分约增加 0.1～0.2 秒延迟。" if self.superres else
                 "提取采样率 16 kHz，音频带宽约 8 kHz；重叠人声和强混响下可能仍有残留。"))

    def refresh_devices(self) -> None:
        if self.busy:
            return
        try:
            # PortAudio enumerates endpoints only on initialization. No streams
            # are active here; a restart makes newly plugged devices visible.
            sd._terminate()
            sd._initialize()
            hosts = sd.query_hostapis()
            self.devices = {}
            inputs, outputs = [], [OBS_LABEL]
            self.devices[OBS_LABEL] = OBS_DEVICE
            for index, info in enumerate(sd.query_devices()):
                host = hosts[info["hostapi"]]["name"]
                # WDM-KS exposes duplicate or often inaccessible kernel endpoints.
                if host == "Windows WDM-KS":
                    continue
                label = f"{index}: {info['name']} [{host}]"
                self.devices[label] = index
                if info["max_input_channels"]:
                    inputs.append(label)
                if info["max_output_channels"]:
                    outputs.append(label)
            self.input_box["values"], self.output_box["values"] = inputs, outputs
            for var, labels, direction in ((self.input_name, inputs, "input"),
                                           (self.output_name, outputs, "output")):
                if var.get() in labels:
                    continue
                default = next((host[f"default_{direction}_device"] for host in hosts
                                if host["name"] == "Windows WASAPI"), -1)
                chosen = next((label for label in labels if self.devices[label] == default), "")
                if direction == "output":
                    chosen = next((label for label in labels if ("cable input" in label.lower()
                                   or "vb-audio virtual cable" in label.lower())
                                   and "16 ch" not in label.lower()
                                   and "WASAPI" in label), chosen)
                var.set(chosen or (labels[0] if labels else ""))
            if self.prefer_cable:
                self._select_cable_output()
            self._update_output_help()
        except Exception as exc:
            messagebox.showerror("读取设备失败", str(exc))

    def _select_cable_output(self):
        labels = self.output_box["values"]
        matching = [label for label in labels if "cable input" in label.lower()
                    or "vb-audio virtual cable" in label.lower()
                    or "cable in " in label.lower()]
        matching.sort(key=lambda label: ("WASAPI" not in label, "16 ch" in label.lower()))
        self.output_name.set(matching[0] if matching else "")

    def _update_output_help(self):
        if self.output_name.get() == OBS_LABEL:
            self.copy_obs_button.pack(side="left", padx=8)
            self.routing_help.set(f"OBS「媒体源」取消「本地文件」，输入 {OBS_URL}；格式 wav，网络缓冲 0 MB。\n"
                                  "可保留桌面音频；此人声源的音频监听设为关闭，即可单独调音和添加滤镜。")
        else:
            self.copy_obs_button.pack_forget()
            self.routing_help.set("虚拟声卡：本程序输出选 CABLE Input，OBS「音频输入捕获」选 CABLE Output。\n"
                                  "桌面音频保留耳机；原麦克风、旧媒体源关闭，此人声源的音频监听设为关闭。")

    def copy_obs_url(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(OBS_URL)
        self.status.set("OBS 直连地址已复制。输出选「OBS: 独立人声」并开始处理后，再添加媒体源。")

    def _restore(self) -> None:
        try:
            saved = json.loads(SETTINGS.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                return
            for var, box, key in ((self.input_name, self.input_box, "input"),
                                  (self.output_name, self.output_box, "output")):
                # Resolve by endpoint name + host API rather than unstable device IDs.
                name = saved.get(key, "")
                for label in box["values"]:
                    if label.partition(": ")[2] == name:
                        var.set(label)
                        break
            self.gain.set(float(saved.get("gain", 0)))
            self.floor.set(float(saved.get("floor", -55)))
            self.denoise.set(bool(saved.get("gpu_denoise" if self.gpu else "denoise", False)))
            self.sr_strength.set(float(saved.get("sr_strength_percent", 70)))
            if self.gpu:
                from gpu_super_resolution import LATENCY_PROFILES
                key = saved.get("latency_mode", "balanced")
                self.latency_mode.set(LATENCY_PROFILES.get(key, LATENCY_PROFILES["balanced"])["label"])
        except (OSError, ValueError, TypeError):
            pass

    def _save(self) -> None:
        DATA.mkdir(parents=True, exist_ok=True)
        try:
            saved = json.loads(SETTINGS.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                saved = {}
        except (OSError, ValueError):
            saved = {}
        saved.update({"input": self.input_name.get().partition(": ")[2],
                 "output": self.output_name.get().partition(": ")[2],
                 "gain": self.gain.get(), "floor": self.floor.get(),
                 "gpu_denoise" if self.gpu else "denoise": self.denoise.get(),
                 "sr_strength_percent": self.sr_strength.get()})
        if self.gpu:
            saved["latency_mode"] = self._latency_key()
        temp = DATA / "settings.pending.json"
        temp.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
        replace_file(temp, SETTINGS)

    def _profile_status(self) -> None:
        if self.personal_model:
            self.profile.set("已选择个人 48 kHz 模型 · 固定识别训练中的你")
            return
        try:
            info = sf.info(PROFILE)
            self.profile.set(f"已保存声纹样本 · {info.duration:.1f} 秒")
        except (OSError, sf.LibsndfileError):
            self.profile.set("未录入声纹")

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        for widget in (self.record_button, self.import_button, self.refresh_button,
                       self.start_button, self.denoise_box, self.collect_button):
            widget.configure(state="disabled" if busy else "normal")
        for widget in (self.input_box, self.output_box):
            widget.configure(state="disabled" if busy else "readonly")
        if self.gpu:
            self.latency_box.configure(state="disabled" if busy else "readonly")
        self.stop_button.configure(state="normal" if busy else "disabled")
        if self.personal_model:
            self.record_button.configure(state="disabled")
            self.import_button.configure(state="disabled")
            self.denoise_box.configure(state="disabled")
            self.denoise.set(False)
        if not busy:
            self.mute_button.configure(state="disabled")

    def open_collector(self) -> None:
        if self.busy:
            return
        from collect_voice import CollectorApp
        window = tk.Toplevel(self.root)
        window.transient(self.root)
        self.collector = CollectorApp(window, self.input_name.get().partition(": ")[2])
        self.set_busy(True)
        self.stop_button.configure(state="disabled")
        self.status.set("训练原声采集窗口已打开。")
        window.grab_set()
        try:
            self.root.wait_window(window)
        finally:
            self.collector = None
            if self.root.winfo_exists():
                self.set_busy(False)
                self.refresh_devices()
                self.status.set("采集已关闭，原声保存在 data/training/captures。")

    def record(self) -> None:
        device = self.devices.get(self.input_name.get())
        if device is None:
            messagebox.showerror("没有输入设备", "请先选择麦克风。")
            return
        self.record_stop.clear()
        self.set_busy(True)
        self.status.set("准备录入：请由指定人单独连续说话…")

        def work():
            try:
                with windows_audio_context():
                    rate, channels = device_format(device, "input")
                    parts = []
                    block = round(rate * 0.04)
                    # Discard the first half-second to avoid the button click/transient.
                    with open_audio_stream("input", device=device, samplerate=rate, channels=channels,
                                           blocksize=block, dtype="float32",
                                           notify=lambda text: self.events.put(("status", text))) as stream:
                        for index in range(163):
                            if self.record_stop.is_set():
                                return
                            audio, overflow = stream.read(block)
                            if overflow:
                                raise RuntimeError("声纹录音发生丢帧，请关闭占用音频的程序后重试。")
                            if index >= 13:
                                parts.append(audio[:, 0].copy())
                            if index % 25 == 0:
                                self.events.put(("status", f"正在录入，请继续说话… {max(0, index - 13) * .04:.0f}/6 秒"))
                    audio = np.concatenate(parts)
                    if rate != SR:
                        audio = soxr.resample(audio, rate, SR).astype(np.float32)
                    if not self.record_stop.is_set():
                        save_sample(audio)
                        self.events.put(("profile", "声纹已录入，可以开始处理。"))
            except Exception as exc:
                LOG.exception("录入失败")
                self.events.put(("error", str(exc)))
            finally:
                self.events.put(("idle", None))

        self.task = threading.Thread(target=work, daemon=True)
        self.task.start()

    def import_sample(self) -> None:
        path = filedialog.askopenfilename(title="选择指定人的干净录音（至少 3 秒）",
                                         filetypes=[("音频文件", "*.wav *.flac *.ogg *.mp3"), ("所有文件", "*.*")])
        if not path:
            return
        self.record_stop.clear()
        self.set_busy(True)
        self.status.set("正在导入声纹样本…")

        def work():
            try:
                # Read only the first 30 s; the extractor selects its loudest 3 s.
                with sf.SoundFile(path) as source:
                    audio = source.read(frames=source.samplerate * 30, dtype="float32", always_2d=True)
                    rate = source.samplerate
                audio = audio.mean(axis=1)
                if rate != SR:
                    audio = soxr.resample(audio, rate, SR).astype(np.float32)
                if not self.record_stop.is_set():
                    save_sample(audio)
                    self.events.put(("profile", "声纹已导入，可以开始处理。"))
            except Exception as exc:
                LOG.exception("导入失败")
                self.events.put(("error", str(exc)))
            finally:
                self.events.put(("idle", None))

        self.task = threading.Thread(target=work, daemon=True)
        self.task.start()

    def start(self) -> None:
        if not self.personal_model and not PROFILE.exists():
            messagebox.showinfo("请先指定人物", "先录入或导入目标人物单独说话的录音。")
            return
        input_device = self.devices.get(self.input_name.get())
        output_device = self.devices.get(self.output_name.get())
        if input_device is None or output_device is None:
            messagebox.showerror("设备未选择", "请选择麦克风和输出设备。")
            return
        try:
            self.update_options()
            self.models.latency_mode = self._latency_key()
            self._save()
        except (ValueError, tk.TclError, OSError) as exc:
            messagebox.showerror("设置无效", str(exc))
            return
        self.set_busy(True)
        self.engine.options.muted = False
        self.mute_button.configure(text="静音（Ctrl+M）")
        self.status.set("正在准备声纹和模型…")
        self.engine.start(input_device, output_device, self.denoise.get())

    def stop(self) -> None:
        self.record_stop.set()
        self.engine.stop()
        self.status.set("正在停止…")

    def toggle_mute(self) -> None:
        if not self.busy or not self.engine.thread or not self.engine.thread.is_alive():
            return
        self.engine.options.muted = not self.engine.options.muted
        self.mute_button.configure(text="恢复声音（Ctrl+M）" if self.engine.options.muted else "静音（Ctrl+M）")

    def update_options(self) -> None:
        gain, floor = float(self.gain.get()), float(self.floor.get())
        if not np.isfinite(gain) or not np.isfinite(floor) or not (-12 <= gain <= 18 and -80 <= floor <= -25):
            raise ValueError("增益范围 -12～18 dB，门限范围 -80～-25 dB。")
        self.engine.options.gain_db, self.engine.options.floor_db = gain, floor
        strength = float(self.sr_strength.get())
        if not np.isfinite(strength) or not 0 <= strength <= 100:
            raise ValueError("高频补全强度范围 0～100%。")
        self.engine.options.sr_strength = strength / 100

    def tick(self) -> None:
        try:
            self.update_options()
        except (ValueError, tk.TclError):
            pass
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                self.status.set(payload)
            elif kind == "running":
                mode = "个人 48 kHz 模型" if self.personal_model else ("声纹提取 + 48 kHz 超分" if self.superres else "已锁定录入声纹")
                self.status.set(f"正在处理 · {mode} · {self.models.backend_name} · 设备 {payload[0]} → {payload[1]} Hz")
                if self.engine.obs_output is not None:
                    self.status.set(self.status.get() + " · OBS 独立人声输出中")
                if self.gpu:
                    self.status.set(self.status.get() + f" · {self.latency_mode.get()}")
                self.mute_button.configure(state="normal")
            elif kind == "error":
                self.status.set("已停止：" + payload)
                if not self.closing:
                    messagebox.showerror("处理失败", payload)
            elif kind == "profile":
                self._profile_status()
                self.status.set(payload)
            elif kind in ("stopped", "idle"):
                self.set_busy(False)
                if self.status.get() == "正在停止…":
                    self.status.set("已停止")
        if self.busy and self.engine.thread and self.engine.thread.is_alive() and self.engine.buffer:
            fifo = self.engine.buffer
            muted = " · 已静音" if self.engine.options.muted else ""
            receivers = (f" · OBS 接收 {self.engine.obs_output.receivers} 路"
                         if self.engine.obs_output is not None else "")
            self.stats.set(f"输入 {self.engine.input_db:.0f} dB · 输出 {self.engine.output_db:.0f} dB · "
                           f"计算负载 {self.engine.rtf:.0%} · 丢帧 {self.engine.drops} · "
                           f"缓冲不足 {fifo.underruns} · 设备异常 {self.engine.device_errors}{receivers}{muted}")
        if self.closing:
            active = ((self.engine.thread and self.engine.thread.is_alive()) or
                      (self.task and self.task.is_alive()))
            if not active:
                self.root.destroy()
                return
        self.root.after(100, self.tick)

    def close(self) -> None:
        if self.collector is not None:
            self.collector.close()
            self.root.after(100, self.close)
            return
        self.closing = True
        self.stop()
        self.root.withdraw()
        # Model downloads may still be in progress; daemon workers must not
        # keep an invisible window running indefinitely when the user exits.
        self.root.after(1500, self.root.destroy)


def main() -> None:
    parser = argparse.ArgumentParser(description="实时麦克风目标人声提取与降噪")
    parser.add_argument("--prepare", action="store_true", help="下载并加载模型后退出")
    parser.add_argument("--devices", action="store_true", help="显示音频设备后退出")
    parser.add_argument("--collect", action="store_true", help="采集 48 kHz 训练原声")
    parser.add_argument("--personal", action="store_true", help="使用训练后的个人 48 kHz 模型")
    parser.add_argument("--superres", action="store_true", help="声纹提取后进行 16→48 kHz 超分")
    parser.add_argument("--gpu", action="store_true", help="CUDA LavaSR v2 高质量超分（通过 start_gpu.bat 启动）")
    parser.add_argument("--obs", action="store_true", help="默认选择 OBS 独立人声输出")
    parser.add_argument("--cable", action="store_true", help="默认选择 CABLE Input 虚拟声卡输出")
    args = parser.parse_args()
    if args.obs and args.cable:
        parser.error("OBS 媒体源与 CABLE 虚拟声卡输出请分别选择。")
    if args.personal and (args.superres or args.gpu):
        parser.error("个人 48 kHz 模型与 16 kHz 超分模式请分别启动。")
    DATA.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=DATA / "errors.log", encoding="utf-8", level=logging.ERROR,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.collect:
        from collect_voice import main as collect_main
        collect_main()
    elif args.devices:
        print(sd.query_devices())
    elif args.prepare:
        Models(DATA / "models" / "personal48k" / "model.onnx" if args.personal else None,
               args.superres, args.gpu).load(print)
        print("模型就绪")
    else:
        root = tk.Tk()
        App(root, DATA / "models" / "personal48k" / "model.onnx" if args.personal else None,
            args.superres, args.gpu, args.obs, args.cable)
        root.mainloop()


if __name__ == "__main__":
    main()
