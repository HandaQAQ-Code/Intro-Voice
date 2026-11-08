from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import urllib.request

import numpy as np
import onnxruntime as ort
from scipy.signal import firwin, lfilter
import soxr

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "data" / "super_resolution"
MODEL = ASSETS / "flashsr_lite.onnx"
REVISION = "2a69326250613c0a0f6c1c8d9f0c48cb779842b8"
MODEL_URL = f"https://raw.githubusercontent.com/ysharma3501/FlashSR/{REVISION}/models/model_lite.onnx"


def prepare_model():
    ASSETS.mkdir(parents=True, exist_ok=True)
    if not MODEL.is_file():
        request = urllib.request.Request(MODEL_URL, headers={"User-Agent": "live-voice-super-resolution"})
        pending = MODEL.with_suffix(".pending.onnx")
        with urllib.request.urlopen(request, timeout=45) as source, pending.open("wb") as destination:
            while chunk := source.read(1024 * 1024):
                destination.write(chunk)
        os.replace(pending, MODEL)
    # Inspect the graph: the HF model has peak normalization in its output,
    # whereas this raw graph preserves the network's actual amplitude.
    session = load_session(MODEL)
    source = {"repository": "https://github.com/ysharma3501/FlashSR", "revision": REVISION,
              "weights": MODEL_URL, "license": "Apache-2.0",
              "sha256": hashlib.sha256(MODEL.read_bytes()).hexdigest(),
              "input_sample_rate": 16000, "output_sample_rate": 48000,
              "streaming": {"hop_ms": 80, "context_ms": 16,
                            "filter_delay_ms": 64 / 48},
              "method": "original_upsampled_low_band_plus_generated_high_band",
              "note": "高频为模型估计，不是原始波形的无损恢复。"}
    metadata = ASSETS / "flashsr_source.json"
    if not metadata.exists():
        metadata.write_text(json.dumps(source, ensure_ascii=False, indent=2), encoding="utf-8")
    return session


def load_session(path: Path = MODEL):
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
    if session.get_inputs()[0].name != "x" or len(session.get_inputs()[0].shape) != 3:
        raise ValueError("超分模型格式不匹配，需要不含峰值归一化的 FlashSR 原始 ONNX。")
    return session


class StreamingSuperResolution:
    """16->48 kHz with fixed context, exact sample accounting and no peak AGC.

    Neural inference always sees the isolated target, never the raw microphone.
    The target's low band is retained by resampling; a high-pass FIR adds only
    the synthesized high band. This is bandwidth extension, not source recovery.
    """

    HOP = 1280
    CONTEXT = 256
    FILTER_DELAY = 64
    RESAMPLE_QUALITY = "MQ"

    def __init__(self, session, *, hop=None, lookahead=None):
        self.HOP = self.HOP if hop is None else int(hop)
        self.lookahead = self.CONTEXT if lookahead is None else int(lookahead)
        if self.HOP <= 0 or self.lookahead < 0:
            raise ValueError("超分分块和前瞻参数无效。")
        self.session = session
        self.pending = np.empty(0, dtype=np.float32)
        self.history = np.zeros(self.CONTEXT, dtype=np.float32)
        self.filter = firwin(129, 8000, fs=48000, pass_zero=False).astype(np.float32)
        self.filter_state = np.zeros(128, dtype=np.float32)
        self.low_delay = np.zeros(self.FILTER_DELAY, dtype=np.float32)
        self.activity_gain = 0.0
        self.n_input = 0
        self.n_output = 0
        self.closed = False

    def _generate(self, context: np.ndarray):
        return self.session.run(None, {"x": context[None, None]})[0].reshape(-1)

    def _bands(self, context, start, end):
        generated = self._generate(context)
        if len(generated) != len(context) * 3:
            raise RuntimeError("超分模型输出长度异常。")
        low = soxr.resample(context, 16000, 48000, quality=self.RESAMPLE_QUALITY)
        return generated[start:end], low[start:end].astype(np.float32)

    def _window(self, current: np.ndarray, strength: float):
        context = np.concatenate([self.history, current])
        start, end = self.CONTEXT * 3, (self.CONTEXT + self.HOP) * 3
        high, low = self._bands(context, start, end)
        if len(high) != self.HOP * 3 or not np.isfinite(high).all() or not np.isfinite(low).all():
            raise RuntimeError("超分模型输出异常，已停止音频。")
        # Suppress the model's silence bias before the high-pass filter. No
        # normalization, compression or noisy raw microphone is mixed back in.
        rms = float(np.sqrt(np.mean(current[:self.HOP].astype(np.float64) ** 2)))
        activity = min(1.0, rms / .001)
        high = high * np.linspace(self.activity_gain, activity, len(high), dtype=np.float32)
        self.activity_gain = activity
        high, self.filter_state = lfilter(self.filter, [1.0], high, zi=self.filter_state)
        delayed = np.concatenate([self.low_delay, low])
        self.low_delay = delayed[-self.FILTER_DELAY:].copy()
        output = delayed[:-self.FILTER_DELAY] + high * float(np.clip(strength, 0, 1))
        self.history = np.concatenate([self.history, current[:self.HOP]])[-self.CONTEXT:].copy()
        self.n_output += len(output)
        return output.astype(np.float32)

    def push(self, audio: np.ndarray, strength: float = .7):
        if self.closed:
            raise RuntimeError("超分流已结束。")
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        if not np.isfinite(audio).all():
            raise ValueError("超分输入包含无效音频。")
        self.n_input += len(audio)
        self.pending = np.concatenate([self.pending, audio])
        outputs = []
        while len(self.pending) >= self.HOP + self.lookahead:
            outputs.append(self._window(self.pending[:self.HOP + self.lookahead], strength))
            self.pending = self.pending[self.HOP:]
        return np.concatenate(outputs) if outputs else np.empty(0, dtype=np.float32)

    def flush(self, strength: float = .7):
        if self.closed:
            return np.empty(0, dtype=np.float32)
        remaining = self.n_input * 3 - self.n_output
        outputs = []
        while sum(map(len, outputs)) < remaining:
            current = np.pad(self.pending, (0, max(0, self.HOP + self.lookahead - len(self.pending))))
            outputs.append(self._window(current[:self.HOP + self.lookahead], strength))
            self.pending = self.pending[min(len(self.pending), self.HOP):]
        self.closed = True
        return np.concatenate(outputs)[:remaining] if outputs else np.empty(0, dtype=np.float32)


def enhance_array(audio: np.ndarray, session, strength: float = .7):
    processor = StreamingSuperResolution(session)
    # Include a short trailing pad, then compensate the linear-phase FIR delay.
    padded = np.pad(np.asarray(audio, np.float32), (0, 32))
    output = np.concatenate([processor.push(padded, strength), processor.flush(strength)])
    return output[processor.FILTER_DELAY:processor.FILTER_DELAY + len(audio) * 3]


if __name__ == "__main__":
    prepare_model()
    print("16→48 kHz 超分模型已就绪：", MODEL)
