from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import queue
import shutil
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from uuid import uuid4

import numpy as np
import sounddevice as sd
import soundfile as sf


ROOT = Path(__file__).resolve().parent
DATASET = ROOT / "data" / "training" / "captures"
RATE = 48000
BLOCK = 960
LOG = logging.getLogger("voice_capture")

# These are topic cues, not transcripts: the user may speak freely.
SPEECH_PROMPTS = (
    "像平时直播一样打个招呼，再说说你今天准备聊什么。语气自然，不用刻意压低声音。",
    "介绍一下你最常用的一件东西：它是什么，为什么喜欢，有什么不方便的地方？",
    "今天下午三点二十五分，我们约了六位朋友见面。把时间、人数换几次，用自己的话说出来。",
    "描述一下窗外的天气：温度、风声、光线，以及你准备穿什么衣服出门。",
    "假设有人问你一个没听清的问题，先反问一句，再认真解释自己的看法。",
    "讲一道你会做的菜，从准备食材到出锅，按照真实的说话节奏来讲。",
    "欢迎刚进来的朋友！如果喜欢今天的话题，可以先留下来听一会儿，稍后我们一起讨论。",
    "讲一个最近让你高兴的小事，允许自然地笑一下，再继续说话。",
    "描述一次购物经历：原价一百二十八元，折后九十六元，运费另算，你觉得值不值？",
    "介绍你住的城市，说说交通、食物和一个值得去看看的地方。",
    "假设直播中有人持不同意见，用平和但坚定的语气回应，解释你的理由。",
    "从一数到二十，再说几组日期、金额和电话号码。停顿和重音保持自然。",
    "说说早起和晚睡各有什么影响，再介绍你最近的作息安排。",
    "描述一个游戏或软件的使用步骤，先做什么，再做什么，最后怎样确认结果。",
    "请把这件事情讲清楚：为什么要这样做，还有没有其他办法，哪些地方需要注意？",
    "回忆一段旅行，说一说出发、途中和到达之后最有印象的事情。",
    "假设朋友来家里做客，问他想喝茶、咖啡还是温水，再介绍一下房间。",
    "这个消息是真的吗？你从哪里看到的？先提出疑问，再讲讲你会如何确认。",
    "讲讲你今天吃了什么，口感如何，有没有下次还想再吃的东西。",
    "用稍微轻一点的正常说话声音介绍一个话题，保持自然，不要刻意耳语。",
    "用平时略有兴奋的语气讲一个好消息，注意别喊叫，也不要让麦克风破音。",
    "说说你正在学习的事情：哪里简单，哪里困难，准备怎样继续练习。",
    "如果今天只有半小时空闲，你打算怎么安排？说说原因和具体步骤。",
    "讲一个小故事，里面可以有两个人的对话，但始终用自己的真实声音。",
    "总结一下刚才的讨论：大家提出了什么问题，你最赞成哪一点，还有什么没想清楚？",
    "介绍一部电影或一本书，不用背台词，讲自己的感受就可以。",
    "谢谢大家刚才的留言。有些问题我需要再想一想，我们先聊下一个话题。",
    "说说周末的计划，包括时间、地点、同行的人，以及天气变化时的备选安排。",
    "选一个你熟悉的话题自由说一段，包含自然的停顿、转折、疑问和解释。",
    "像正常结束直播那样总结今天的内容，向大家道别，再说说下次想聊的事情。",
)

SINGING_PROMPTS = (
    "清唱一小段你熟悉的旋律，保持平时唱歌的音量，不要用伴奏或混响。",
    "用舒适的音域唱一段连贯旋律，包含低音、中音和自然换气。",
    "同一段旋律唱两遍：第一遍轻声，第二遍用平时直播的音量。不要喊叫。",
    "唱一段有长音和短句的旋律，保持真实的咬字和停顿。",
    "哼唱一段旋律后，立刻用普通说话介绍刚才的感觉。",
    "自由清唱约十秒，再用自然说话说一句你想表达的话。",
)
PROMPTS = SPEECH_PROMPTS
MODE_LABELS = ("说话直播（推荐）", "唱歌补充", "混合素材")


def prompt_bank(mode: str, index: int) -> tuple[str, str]:
    if mode == "唱歌补充":
        return "singing", SINGING_PROMPTS[index % len(SINGING_PROMPTS)]
    if mode == "混合素材" and index % 3 == 2:
        return "singing", SINGING_PROMPTS[(index // 3) % len(SINGING_PROMPTS)]
    return "speech", SPEECH_PROMPTS[index % len(SPEECH_PROMPTS)]



def atomic_json(path: Path, value) -> None:
    temp = path.with_name(f".{path.stem}.{uuid4().hex}.pending.json")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    os.replace(temp, path)


def quality_report(audio: np.ndarray, dropped: bool = False) -> dict:
    x = np.asarray(audio, dtype=np.float32).reshape(-1)
    if not len(x) or not np.isfinite(x).all():
        raise ValueError("录音为空或包含无效音频数据。")
    peak = float(np.max(np.abs(x)))
    rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    frames = x[:len(x) // BLOCK * BLOCK].reshape(-1, BLOCK)
    frame_rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-12) if len(frames) else np.zeros(0)
    active_seconds = float(np.count_nonzero(frame_rms > 10 ** (-48 / 20)) * BLOCK / RATE)
    clipped = float(np.mean(np.abs(x) >= 0.995))
    problems = []
    if dropped:
        problems.append("录音丢帧")
    if len(x) < RATE * 3:
        problems.append("录音不足 3 秒")
    if clipped > 0.0001:
        problems.append("削波，请降低麦克风增益")
    if rms < 10 ** (-45 / 20) or active_seconds < 2:
        problems.append("电平过低或声音太少")
    return {"peak_dbfs": round(20 * np.log10(peak + 1e-12), 2),
            "rms_dbfs": round(20 * np.log10(rms + 1e-12), 2),
            "clipped_fraction": clipped,
            "active_seconds_estimate": round(active_seconds, 2),
            "dropped_frames": dropped, "problems": problems}


class CaptureSession:
    def __init__(self, root: Path, source: dict, split: str, mode: str):
        self.id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid4().hex[:8]
        self.path = root / self.id
        self.path.mkdir(parents=True, exist_ok=False)
        self.source = source
        self.split = split
        self.mode = mode
        atomic_json(self.path / "session.json", {
            "schema_version": 1, "session_id": self.id,
            "created_at": datetime.now().astimezone().isoformat(),
            "speaker_id": "target", "split": split, "audio_mode": mode, "source": source,
            "sample_rate": RATE, "channels": 1, "subtype": "FLOAT",
            "processing": [], "purpose": "clean_target_speech",
        })

    def save(self, audio: np.ndarray, prompt: str, mode: str, complete: bool, dropped: bool) -> dict:
        report = quality_report(audio, dropped)
        stem = "take_" + uuid4().hex
        path = self.path / f"{stem}.wav"
        pending = self.path / f"{stem}.pending.wav"
        # IEEE float WAV preserves the captured float32 samples exactly.
        # No resampling, denoising, normalization, silence removal or gate.
        sf.write(pending, audio, RATE, subtype="FLOAT")
        os.replace(pending, path)
        row = {"schema_version": 1, "session_id": self.id, "speaker_id": "target",
               "split": self.split, "audio_mode": self.mode, "content_type": mode,
               "audio": path.name, "created_at": datetime.now().astimezone().isoformat(),
               "sample_rate": RATE, "channels": 1, "subtype": "FLOAT",
               "frames": len(audio), "duration_seconds": len(audio) / RATE,
               "source": self.source, "processing": [],
               "prompt": prompt, "prompt_is_transcript": False,
               "complete_segment": complete,
               "usable": not report["problems"], "quality": report}
        atomic_json(path.with_suffix(".json"), row)
        rebuild_manifest(self.path)
        return row | {"path": str(path)}


def rebuild_manifest(session: Path) -> None:
    rows = []
    for path in sorted(session.glob("take_*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        if (session / row["audio"]).is_file():
            rows.append(row)
    temp = session / f".manifest.{uuid4().hex}.pending.jsonl"
    temp.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    destination = session / "manifest.jsonl"
    if destination.exists():
        shutil.copy2(destination, destination.with_suffix(".jsonl.bak"))
    os.replace(temp, destination)


def exclude_take(path: Path) -> None:
    meta = path.with_suffix(".json")
    row = json.loads(meta.read_text(encoding="utf-8"))
    row["usable"] = False
    row["excluded_by_user"] = True
    atomic_json(meta, row)
    rebuild_manifest(path.parent)


def dataset_totals(root: Path) -> tuple[int, float, float]:
    count, seconds, usable = 0, 0.0, 0.0
    for path in root.glob("*/take_*.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            if not (path.parent / row["audio"]).is_file():
                continue
            duration = float(row["duration_seconds"])
            count += 1
            seconds += duration
            if row["usable"]:
                usable += duration
        except (OSError, ValueError, KeyError, TypeError):
            LOG.warning("跳过损坏的片段元数据：%s", path)
    return count, seconds, usable


def capture_format(device: int, channel: int) -> tuple[int, dict]:
    info = sd.query_devices(device)
    host = sd.query_hostapis(info["hostapi"])["name"]
    # Reject endpoints that would require upsampling. A 48 kHz container cannot
    # restore high frequencies missing from a narrower-band device/transport.
    if int(info["default_samplerate"]) != RATE:
        raise ValueError("此设备的系统采样率不是 48 kHz。请在 Windows 声音设置中选 48000 Hz，"
                         "然后刷新设备；程序不会把低采样率录音补成 48 kHz。")
    if host != "Windows WASAPI" and os.name == "nt":
        raise ValueError("原声采集请使用 Windows WASAPI 麦克风。")
    channels = min(2, int(info["max_input_channels"]))
    if channels < 1 or not 0 <= channel < channels:
        raise ValueError("请选择设备上存在的输入声道。")
    extra = sd.WasapiSettings(exclusive=False, auto_convert=False) if host == "Windows WASAPI" else None
    sd.check_input_settings(device=device, samplerate=RATE, channels=channels,
                            dtype="float32", extra_settings=extra)
    return channels, {"device_name": info["name"], "host_api": host,
                      "device_default_sample_rate": info["default_samplerate"],
                      "capture_channels": channels, "selected_channel": channel + 1}


@contextmanager
def raw_input_stream(device: int, channel: int):
    # Reuse the COM/startup fixes without importing or loading inference models
    # until the user actually starts recording.
    from live_voice import open_audio_stream, windows_audio_context
    with windows_audio_context():
        channels, source = capture_format(device, channel)
        host_options = (sd.WasapiSettings(exclusive=False, auto_convert=False)
                        if source["host_api"] == "Windows WASAPI" else None)
        with open_audio_stream("input", device=device, samplerate=RATE, channels=channels,
                               dtype="float32", blocksize=BLOCK,
                               extra_settings=host_options) as stream:
            yield stream, source


class CaptureWorker:
    def __init__(self, events: queue.Queue, root: Path = DATASET):
        self.events = events
        self.root = root
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self, device: int, channel: int, seconds: int, continuous: bool, split: str, prompt_index: int, mode: str):
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run,
            args=(device, channel, seconds, continuous, split, prompt_index, mode), daemon=True)
        self.thread.start()

    def _run(self, device: int, channel: int, seconds: int, continuous: bool, split: str, prompt_index: int, mode: str):
        try:
            with raw_input_stream(device, channel) as (stream, source):
                session = CaptureSession(self.root, source, split, mode)
                index = prompt_index
                self.events.put(("session", str(session.path)))
                while not self.stop_event.is_set():
                    prompt_type, prompt = prompt_bank(mode, index)
                    self.events.put(("prompt", (index, prompt_type, prompt)))
                    # Read and discard during the preparation interval so the
                    # capture buffer cannot accumulate old audio or button clicks.
                    for block in range(100):
                        if self.stop_event.is_set():
                            return
                        stream.read(BLOCK)
                        if block % 5 == 0:
                            self.events.put(("preparing", 2 - block * BLOCK / RATE))
                    parts, dropped = [], False
                    for block in range(seconds * RATE // BLOCK):
                        if self.stop_event.is_set():
                            break
                        audio, overflow = stream.read(BLOCK)
                        mono = audio[:, channel].copy()
                        if not np.isfinite(mono).all():
                            raise RuntimeError("设备返回了无效音频，请检查麦克风连接。")
                        parts.append(mono)
                        dropped = dropped or overflow
                        if block % 5 == 0:
                            peak = float(np.max(np.abs(mono)))
                            rms = float(np.sqrt(np.mean(mono ** 2)))
                            self.events.put(("level", ((block + 1) * BLOCK / RATE,
                                20 * np.log10(rms + 1e-12), 20 * np.log10(peak + 1e-12))))
                    if parts:
                        audio = np.concatenate(parts)
                        row = session.save(audio, prompt, prompt_type, len(audio) == seconds * RATE, dropped)
                        self.events.put(("saved", row))
                    if not continuous:
                        break
                    index += 1
        except Exception as exc:
            LOG.exception("采集失败")
            self.events.put(("error", str(exc)))
        finally:
            self.events.put(("idle", None))


class CollectorApp:
    def __init__(self, root: tk.Tk | tk.Toplevel, input_identity: str = ""):
        self.root = root
        root.title("48 kHz 训练原声采集 · 说话直播")
        root.geometry("860x810")
        root.minsize(810, 790)
        self.events = queue.Queue()
        self.worker = CaptureWorker(self.events)
        self.busy = self.closing = False
        self.last_take: Path | None = None
        self.prompt_index = dataset_totals(DATASET)[0] % len(SPEECH_PROMPTS)
        self.devices = {}
        self.input_name = tk.StringVar()
        self.channel = tk.StringVar(value="1")
        self.seconds = tk.IntVar(value=12)
        self.continuous = tk.BooleanVar(value=True)
        self.split = tk.StringVar(value="训练集")
        self.audio_mode = tk.StringVar(value=MODE_LABELS[0])
        self.status = tk.StringVar(value="准备好后开始采集；每段之间留 2 秒准备下一段。")
        self.prompt = tk.StringVar(value=prompt_bank(self.audio_mode.get(), self.prompt_index)[1])
        self.level = tk.StringVar(value="尚未录音")
        self.totals = tk.StringVar()
        self.last_result = tk.StringVar(value="没有录音片段")
        self._build()
        self.refresh(input_identity)
        self.refresh_totals()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.tick)

    def _build(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=7)
        frame = ttk.Frame(self.root, padding=22)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text="为你的声音采集训练原声", font=("Microsoft YaHei UI", 19, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 12))
        ttk.Label(frame, text="48 kHz · 单声道 · 32 位浮点 WAV · 本程序不做降噪、增益或重采样",
                  foreground="#245f47").grid(row=1, column=0, sticky="w")
        ttk.Label(frame, text="安静环境只由你说话。关闭麦克风、WO Mic 等软件中的音效和降噪。\n"
                  "设备或传输本身若已压缩/限频，保存成 48 kHz 也无法恢复原有细节。",
                  wraplength=750, foreground="#526477").grid(row=2, column=0, sticky="ew", pady=(8, 12))
        ttk.Label(frame, text="麦克风（WASAPI）").grid(row=3, column=0, sticky="w")
        self.input_box = ttk.Combobox(frame, state="readonly", textvariable=self.input_name)
        self.input_box.grid(row=4, column=0, sticky="ew", pady=5)
        row = ttk.Frame(frame)
        row.grid(row=5, column=0, sticky="w", pady=(4, 12))
        self.refresh_button = ttk.Button(row, text="刷新设备", command=self.refresh)
        self.refresh_button.pack(side="left")
        ttk.Label(row, text="输入声道").pack(side="left", padx=(15, 5))
        self.channel_box = ttk.Combobox(row, state="readonly", width=3, values=("1", "2"), textvariable=self.channel)
        self.channel_box.pack(side="left")
        ttk.Label(row, text="每段秒数").pack(side="left", padx=(15, 5))
        self.duration_box = ttk.Spinbox(row, from_=5, to=30, width=4, textvariable=self.seconds)
        self.duration_box.pack(side="left")
        self.split_box = ttk.Combobox(row, width=14, state="readonly", textvariable=self.split,
                                     values=("训练集", "验证集（独立场次）"))
        self.split_box.pack(side="left", padx=(15, 0))
        self.mode_box = ttk.Combobox(row, width=18, state="readonly", textvariable=self.audio_mode,
                                     values=MODE_LABELS)
        self.mode_box.pack(side="left", padx=(15, 0))
        self.mode_box.bind("<<ComboboxSelected>>", lambda _event: self.update_prompt())
        cue = ttk.LabelFrame(frame, text="提示 / 自由发挥（不用逐字照读）", padding=15)
        cue.grid(row=6, column=0, sticky="ew")
        ttk.Label(cue, textvariable=self.prompt, wraplength=710,
                  font=("Microsoft YaHei UI", 14)).pack(fill="x", pady=6)
        self.next_button = ttk.Button(cue, text="换个话题", command=self.next_prompt)
        self.next_button.pack(anchor="w", pady=(6, 0))
        self.continuous_box = ttk.Checkbutton(frame, text="连续采集（自动分段，随时停止并保存当前段）",
                                             variable=self.continuous)
        self.continuous_box.grid(row=7, column=0, sticky="w", pady=12)
        controls = ttk.Frame(frame)
        controls.grid(row=8, column=0, sticky="w")
        self.start_button = ttk.Button(controls, text="开始采集", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="停止并保存", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.exclude_button = ttk.Button(controls, text="排除上一段", command=self.exclude_last, state="disabled")
        self.exclude_button.pack(side="left")
        ttk.Button(controls, text="打开录音文件夹", command=self.open_folder).pack(side="left", padx=8)
        self.progress = ttk.Progressbar(frame, maximum=12)
        self.progress.grid(row=9, column=0, sticky="ew", pady=(18, 5))
        ttk.Label(frame, textvariable=self.level).grid(row=10, column=0, sticky="w")
        ttk.Label(frame, textvariable=self.status, wraplength=750).grid(row=11, column=0, sticky="ew", pady=8)
        ttk.Label(frame, textvariable=self.last_result, wraplength=750,
                  foreground="#526477").grid(row=12, column=0, sticky="ew")
        ttk.Label(frame, textvariable=self.totals).grid(row=13, column=0, sticky="w", pady=8)
        ttk.Label(frame, text="先收集 3～5 分钟普通说话即可做第一次试训，建议逐步积累到 5～10 分钟；唱歌可选作音色补充。\n"
                  "质量检查仅筛查电平、削波和丢帧；有他人说话、音乐或明显噪声时请排除该段。",
                  wraplength=750, foreground="#526477").grid(row=14, column=0, sticky="ew", pady=(4, 0))

    def refresh(self, input_identity=""):
        if self.busy:
            return
        old = input_identity or self.input_name.get().partition(": ")[2]
        try:
            sd._terminate()
            sd._initialize()
            hosts = sd.query_hostapis()
            self.devices = {}
            for index, info in enumerate(sd.query_devices()):
                host = hosts[info["hostapi"]]["name"]
                if not info["max_input_channels"] or (os.name == "nt" and host != "Windows WASAPI"):
                    continue
                label = f"{index}: {info['name']} [{host}]"
                self.devices[label] = index
            self.input_box["values"] = list(self.devices)
            default = next((host["default_input_device"] for host in hosts if host["name"] == "Windows WASAPI"), -1)
            chosen = next((label for label in self.devices if label.partition(": ")[2] == old), "")
            chosen = chosen or next((label for label, index in self.devices.items() if index == default), "")
            self.input_name.set(chosen or next(iter(self.devices), ""))
        except Exception as exc:
            messagebox.showerror("读取设备失败", str(exc), parent=self.root)

    def refresh_totals(self):
        count, total, usable = dataset_totals(DATASET)
        self.totals.set(f"已保存 {count} 段 · 原声 {total / 60:.1f} 分钟 · 通过电平检查 {usable / 60:.1f} 分钟")

    def update_prompt(self):
        _kind, text = prompt_bank(self.audio_mode.get(), self.prompt_index)
        self.prompt.set(text)

    def next_prompt(self):
        self.prompt_index = (self.prompt_index + 1) % len(SPEECH_PROMPTS)
        self.update_prompt()

    def set_busy(self, busy):
        self.busy = busy
        for widget in (self.start_button, self.refresh_button, self.next_button,
                       self.continuous_box, self.duration_box, self.mode_box):
            widget.configure(state="disabled" if busy else "normal")
        for widget in (self.input_box, self.channel_box, self.split_box, self.mode_box):
            widget.configure(state="disabled" if busy else "readonly")
        self.stop_button.configure(state="normal" if busy else "disabled")
        self.exclude_button.configure(state="normal" if self.last_take and not busy else "disabled")

    def start(self):
        try:
            device = self.devices.get(self.input_name.get())
            if device is None:
                raise ValueError("请选择麦克风。")
            seconds = int(self.seconds.get())
            channel = int(self.channel.get()) - 1
            if not 5 <= seconds <= 30:
                raise ValueError("每段录音长度请设为 5～30 秒。")
            self.progress["maximum"] = seconds
            self.progress["value"] = 0
            self.status.set("正在打开 48 kHz 麦克风…")
            self.set_busy(True)
            self.worker.start(device, channel, seconds, self.continuous.get(),
                              "validation" if self.split.get().startswith("验证") else "train",
                              self.prompt_index, self.audio_mode.get())
        except (ValueError, tk.TclError) as exc:
            messagebox.showerror("设置无效", str(exc), parent=self.root)

    def stop(self):
        self.worker.stop_event.set()
        if self.busy:
            self.status.set("正在停止并保存当前原声…")

    def exclude_last(self):
        if not self.last_take or self.busy:
            return
        try:
            exclude_take(self.last_take)
            self.last_result.set("上一段已从训练清单中排除，原始 WAV 仍保留。")
            self.last_take = None
            self.exclude_button.configure(state="disabled")
            self.refresh_totals()
        except (OSError, ValueError) as exc:
            messagebox.showerror("排除失败", str(exc), parent=self.root)

    def open_folder(self):
        DATASET.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(DATASET)

    def tick(self):
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "prompt":
                self.prompt_index, prompt_type, text = value
                self.prompt.set(("唱歌补充：" if prompt_type == "singing" else "说话：") + text)
                self.progress["value"] = 0
            elif kind == "preparing":
                self.status.set(f"准备下一段，请先看提示… {value:.1f} 秒")
            elif kind == "level":
                seconds, rms, peak = value
                self.progress["value"] = seconds
                self.level.set(f"当前 {seconds:.1f} 秒 · 平均 {rms:.0f} dBFS · 峰值 {peak:.0f} dBFS")
                self.status.set("正在采集原声，请自然说话…" if peak < -1 else "声音接近削波，请降低麦克风增益。")
            elif kind == "saved":
                self.last_take = Path(value["path"])
                problems = value["quality"]["problems"]
                verdict = "通过电平检查" if not problems else "建议重录：" + "；".join(problems)
                self.last_result.set(f"已保存 {value['duration_seconds']:.1f} 秒 · {verdict}")
                self.refresh_totals()
            elif kind == "error":
                self.status.set("采集停止：" + value)
                if not self.closing:
                    messagebox.showerror("采集失败", value, parent=self.root)
            elif kind == "idle":
                self.set_busy(False)
                if not self.status.get().startswith("采集停止："):
                    self.status.set("已停止并保存。可继续采集，新场次会自动保留之前的录音。")
        if self.closing and not (self.worker.thread and self.worker.thread.is_alive()):
            self.root.destroy()
            return
        self.root.after(100, self.tick)

    def close(self):
        self.closing = True
        self.stop()
        self.root.withdraw()


def main():
    logs = ROOT / "data"
    logs.mkdir(exist_ok=True)
    logging.basicConfig(filename=logs / "capture_errors.log", encoding="utf-8", level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    root = tk.Tk()
    CollectorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
