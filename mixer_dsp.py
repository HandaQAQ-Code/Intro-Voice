"""Record Out 内置效果器引擎（实时安全）。

设计约束（对应路线图 §3）：
  * 滤波器用 RBJ Cookbook 系数 + scipy.signal.lfilter 流式处理（C 实现、带状态），
    不在音频回调里做 Python per-sample 滤波。
  * 动态类（压缩器 / 门限）的包络检测是每块一次短循环（48 kHz 下 1920 次 ≈ 0.5 ms），
    其余计算全部向量化。
  * 参数变化采用「双滤波器 crossfade」：旧系数与新系数各算一遍，块内线性混合，
    避免 UI 拖动滑块时产生爆音。
  * 所有系数计算带边界保护（频率/Q/增益限幅），输出做 NaN 检查，异常时回退原样。
  * 无 UI 依赖、无日志、无锁等待：set_params 由控制线程写，process 在音频线程读。
"""
from __future__ import annotations

import copy
import math
import threading

import numpy as np
from scipy.signal import lfilter

PARAMS_VERSION = 1


def default_params() -> dict:
    return {
        "version": PARAMS_VERSION,
        "enabled": False,
        "hpf": {"enabled": False, "freq": 80.0, "slope": 12},
        "lpf": {"enabled": False, "freq": 16000.0, "slope": 12},
        "eq": {"enabled": False, "lowDb": 0.0, "midDb": 0.0, "highDb": 0.0,
               "midFreq": 1000.0, "midQ": 1.0},
        "comp": {"enabled": False, "thresholdDb": -18.0, "ratio": 3.0,
                 "attackMs": 8.0, "releaseMs": 120.0, "makeupDb": 0.0, "kneeDb": 6.0},
        "gate": {"enabled": False, "thresholdDb": -50.0, "attackMs": 2.0,
                 "holdMs": 80.0, "releaseMs": 150.0, "rangeDb": -18.0},
    }


# 每个可调参数的合法区间（UI 滑块边界与协议校验共用同一份定义）。
LIMITS: dict[str, tuple] = {
    "hpf.freq": (20.0, 1000.0),
    "hpf.slope": ("choice", (12, 24)),
    "lpf.freq": (2000.0, 20000.0),
    "lpf.slope": ("choice", (12, 24)),
    "eq.lowDb": (-18.0, 18.0),
    "eq.midDb": (-18.0, 18.0),
    "eq.highDb": (-18.0, 18.0),
    "eq.midFreq": (200.0, 6000.0),
    "eq.midQ": (0.2, 6.0),
    "comp.thresholdDb": (-60.0, 0.0),
    "comp.ratio": (1.0, 20.0),
    "comp.attackMs": (0.1, 200.0),
    "comp.releaseMs": (10.0, 1000.0),
    "comp.makeupDb": (-12.0, 24.0),
    "comp.kneeDb": (0.0, 18.0),
    "gate.thresholdDb": (-80.0, 0.0),
    "gate.attackMs": (0.2, 100.0),
    "gate.holdMs": (0.0, 500.0),
    "gate.releaseMs": (5.0, 1000.0),
    "gate.rangeDb": (-60.0, 0.0),
}


def _coerce(path: str, value):
    """按 LIMITS 校验单值：区间外夹取，类型不对或无法解析则返回 None（保留旧值）。"""
    spec = LIMITS.get(path)
    if spec is None:
        return None
    if isinstance(spec[0], str) and spec[0] == "choice":
        try:
            num = int(round(float(value)))
        except (TypeError, ValueError):
            return None
        return num if num in spec[1] else None
    if isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return float(max(spec[0], min(spec[1], num)))


def merge_params(base: dict, override: dict) -> dict:
    """版本化合并：只覆盖已知键，未知键忽略、越界值夹取，保证旧预设向前兼容。"""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for key, value in (override or {}).items():
        if key not in out:
            continue
        if isinstance(out[key], dict):
            if not isinstance(value, dict):
                continue
            for sub, subval in value.items():
                if sub not in out[key]:
                    continue
                if isinstance(out[key][sub], bool):
                    if isinstance(subval, bool):
                        out[key][sub] = subval
                    continue
                coerced = _coerce(f"{key}.{sub}", subval)
                if coerced is not None:
                    out[key][sub] = coerced
        elif isinstance(out[key], bool):
            if isinstance(value, bool):
                out[key] = value
    return out


def _clip(value, lo, hi):
    return max(lo, min(hi, value))


def _design(kind: str, freq: float, rate: int, gain_db: float = 0.0, q: float = 0.707):
    """RBJ Audio EQ Cookbook，返回归一化 (b, a)。"""
    freq = _clip(float(freq), 10.0, rate * 0.5 - 100.0)
    q = _clip(float(q), 0.1, 12.0)
    gain_db = _clip(float(gain_db), -24.0, 24.0)
    w0 = 2.0 * math.pi * freq / rate
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2.0 * q)
    A = 10.0 ** (gain_db / 40.0)

    if kind == "peaking":
        b = [1 + alpha * A, -2 * cw, 1 - alpha * A]
        a = [1 + alpha / A, -2 * cw, 1 - alpha / A]
    elif kind == "lowshelf":
        alpha = sw / 2.0 * math.sqrt(2.0)  # S = 1
        sq = 2.0 * math.sqrt(A) * alpha
        b = [A * ((A + 1) - (A - 1) * cw + sq),
             2 * A * ((A - 1) - (A + 1) * cw),
             A * ((A + 1) - (A - 1) * cw - sq)]
        a = [(A + 1) + (A - 1) * cw + sq,
             -2 * ((A - 1) + (A + 1) * cw),
             (A + 1) + (A - 1) * cw - sq]
    elif kind == "highshelf":
        alpha = sw / 2.0 * math.sqrt(2.0)
        sq = 2.0 * math.sqrt(A) * alpha
        b = [A * ((A + 1) + (A - 1) * cw + sq),
             -2 * A * ((A - 1) + (A + 1) * cw),
             A * ((A + 1) + (A - 1) * cw - sq)]
        a = [(A + 1) - (A - 1) * cw + sq,
             2 * ((A - 1) - (A + 1) * cw),
             (A + 1) - (A - 1) * cw - sq]
    elif kind == "highpass":
        b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]
        a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "lowpass":
        b = [(1 - cw) / 2, 1 - cw, (1 - cw) / 2]
        a = [1 + alpha, -2 * cw, 1 - alpha]
    else:
        raise ValueError(f"未知滤波器类型：{kind}")

    a0 = a[0]
    if abs(a0) < 1e-12:
        a0 = 1e-12
    b = [v / a0 for v in b]
    a = [v / a0 for v in a]
    return np.asarray(b, np.float64), np.asarray(a, np.float64)


class Biquad:
    """立体声双二阶滤波器，参数切换用块内 crossfade 防爆音。"""

    def __init__(self, rate: int, channels: int = 2):
        self.rate = rate
        self.channels = channels
        self.b: np.ndarray | None = None
        self.a: np.ndarray | None = None
        self.zi: np.ndarray | None = None
        self.old_b = self.old_a = self.old_zi = None
        self._pending_fade = False
        self.active = False

    def configure(self, kind: str, freq: float, gain_db: float = 0.0, q: float = 0.707):
        b, a = _design(kind, freq, self.rate, gain_db, q)
        if self.b is None:
            self.b, self.a = b, a
            self.zi = np.zeros((2, self.channels))
            return
        if np.allclose(b, self.b) and np.allclose(a, self.a):
            return
        self.old_b, self.old_a, self.old_zi = self.b, self.a, self.zi
        self.b, self.a = b, a
        self._pending_fade = True

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.b is None:
            return x
        if self._pending_fade:
            y_new, self.zi = lfilter(self.b, self.a, x, axis=0, zi=self.zi)
            y_old, self.old_zi = lfilter(self.old_b, self.old_a, x, axis=0, zi=self.old_zi)
            w = np.linspace(0.0, 1.0, len(x), dtype=np.float64)[:, None]
            self._pending_fade = False
            return (y_old * (1 - w) + y_new * w).astype(np.float32)
        y, self.zi = lfilter(self.b, self.a, x, axis=0, zi=self.zi)
        return y.astype(np.float32)


class HiLoCut:
    """高通 + 低通，slope 12 = 单级 Butterworth，24 = 两级级联。"""

    def __init__(self, rate: int, channels: int):
        self.rate = rate
        self.hp = [Biquad(rate, channels), Biquad(rate, channels)]
        self.lp = [Biquad(rate, channels), Biquad(rate, channels)]
        self.hp_enabled = False
        self.lp_enabled = False

    def configure(self, hp: dict, lp: dict):
        self.hp_enabled = bool(hp.get("enabled"))
        self.lp_enabled = bool(lp.get("enabled"))
        hp_freq = _clip(float(hp.get("freq", 80.0)), 20.0, 1000.0)
        lp_freq = _clip(float(lp.get("freq", 16000.0)), 2000.0, min(20000.0, self.rate * 0.45))
        hp_stages = 2 if int(hp.get("slope", 12)) == 24 else 1
        lp_stages = 2 if int(lp.get("slope", 12)) == 24 else 1
        for i, bq in enumerate(self.hp):
            bq.active = self.hp_enabled and i < hp_stages
            if bq.active:
                bq.configure("highpass", hp_freq, q=0.7071)
        for i, bq in enumerate(self.lp):
            bq.active = self.lp_enabled and i < lp_stages
            if bq.active:
                bq.configure("lowpass", lp_freq, q=0.7071)

    def process(self, x: np.ndarray) -> np.ndarray:
        for bq in self.hp:
            if bq.active:
                x = bq.process(x)
        for bq in self.lp:
            if bq.active:
                x = bq.process(x)
        return x


class ThreeBandEQ:
    """低 shelf / 中 peaking / 高 shelf，中频频率与 Q 可调。"""

    def __init__(self, rate: int, channels: int):
        self.rate = rate
        self.low = Biquad(rate, channels)
        self.mid = Biquad(rate, channels)
        self.high = Biquad(rate, channels)
        self.enabled = False

    def configure(self, cfg: dict):
        self.enabled = bool(cfg.get("enabled"))
        if not self.enabled:
            return
        self.low.configure("lowshelf", 200.0, float(cfg.get("lowDb", 0.0)), 0.7071)
        self.mid.configure("peaking", _clip(float(cfg.get("midFreq", 1000.0)), 200.0, 6000.0),
                           float(cfg.get("midDb", 0.0)), _clip(float(cfg.get("midQ", 1.0)), 0.2, 6.0))
        self.high.configure("highshelf", 4000.0, float(cfg.get("highDb", 0.0)), 0.7071)

    def process(self, x: np.ndarray) -> np.ndarray:
        if not self.enabled:
            return x
        return self.high.process(self.mid.process(self.low.process(x)))


class _Envelope:
    """attack/release 不对称包络（检测侧共用）。每块一次短循环，约 0.5 ms/40 ms。"""

    def __init__(self, rate: int):
        self.rate = rate
        self.value = 0.0
        self.att = 0.0
        self.rel = 0.0

    def set_times(self, attack_ms: float, release_ms: float):
        self.att = math.exp(-1.0 / (self.rate * _clip(attack_ms, 0.1, 500.0) / 1000.0))
        self.rel = math.exp(-1.0 / (self.rate * _clip(release_ms, 1.0, 2000.0) / 1000.0))

    def run(self, peak: np.ndarray) -> np.ndarray:
        out = np.empty(len(peak), np.float64)
        e = self.value
        att, rel = self.att, self.rel
        for i in range(len(peak)):
            p = peak[i]
            coef = att if p > e else rel
            e = coef * e + (1.0 - coef) * p
            out[i] = e
        self.value = e
        return out


class Compressor:
    """阈值 / ratio / attack / release / makeup，soft knee，输出增益衰减供 UI 显示。"""

    def __init__(self, rate: int):
        self.rate = rate
        self.env = _Envelope(rate)
        self.enabled = False
        self.threshold_db = -18.0
        self.ratio = 3.0
        self.makeup = 1.0
        self.knee = 6.0
        self.gr_db = 0.0

    def configure(self, cfg: dict):
        self.enabled = bool(cfg.get("enabled"))
        self.threshold_db = _clip(float(cfg.get("thresholdDb", -18.0)), -60.0, 0.0)
        self.ratio = _clip(float(cfg.get("ratio", 3.0)), 1.0, 20.0)
        self.env.set_times(float(cfg.get("attackMs", 8.0)), float(cfg.get("releaseMs", 120.0)))
        self.makeup = 10.0 ** (_clip(float(cfg.get("makeupDb", 0.0)), -12.0, 24.0) / 20.0)
        self.knee = _clip(float(cfg.get("kneeDb", 6.0)), 0.0, 18.0)

    def process(self, x: np.ndarray) -> np.ndarray:
        if not self.enabled or not len(x):
            return x
        peak = np.max(np.abs(x), axis=1)
        env = self.env.run(peak)
        env_db = 20.0 * np.log10(env + 1e-9)
        over = env_db - self.threshold_db
        slope = 1.0 - 1.0 / self.ratio
        knee = self.knee
        if knee > 1e-3:
            reduction = np.where(
                over <= -knee / 2, 0.0,
                np.where(over >= knee / 2, over * slope,
                         slope * (over + knee / 2) ** 2 / (2 * knee)))
        else:
            reduction = np.maximum(0.0, over) * slope
        gain = np.power(10.0, -reduction / 20.0) * self.makeup
        self.gr_db = float(np.max(reduction))
        return (x * gain[:, None]).astype(np.float32)


class Gate:
    """门限 / 扩展器：阈值 + hold + release，默认温和，避免切字和吃尾音。"""

    def __init__(self, rate: int):
        self.rate = rate
        self.env = _Envelope(rate)
        self.enabled = False
        self.threshold_db = -50.0
        self.range_gain = 10.0 ** (-18.0 / 20.0)
        self.hold_samples = 0
        self.hold_left = 0
        self.gain = 1.0
        self.att = 0.0
        self.rel = 0.0
        self.open = True

    def configure(self, cfg: dict):
        self.enabled = bool(cfg.get("enabled"))
        self.threshold_db = _clip(float(cfg.get("thresholdDb", -50.0)), -80.0, 0.0)
        self.range_gain = 10.0 ** (_clip(float(cfg.get("rangeDb", -18.0)), -60.0, 0.0) / 20.0)
        attack = _clip(float(cfg.get("attackMs", 2.0)), 0.2, 100.0)
        release = _clip(float(cfg.get("releaseMs", 150.0)), 5.0, 1000.0)
        hold = _clip(float(cfg.get("holdMs", 80.0)), 0.0, 500.0)
        self.env.set_times(attack, release)
        self.att = math.exp(-1.0 / (self.rate * attack / 1000.0))
        self.rel = math.exp(-1.0 / (self.rate * release / 1000.0))
        self.hold_samples = int(self.rate * hold / 1000.0)

    def process(self, x: np.ndarray) -> np.ndarray:
        if not self.enabled or not len(x):
            return x
        peak = np.max(np.abs(x), axis=1)
        env = self.env.run(peak)
        threshold = 10.0 ** (self.threshold_db / 20.0)
        gains = np.empty(len(env), np.float64)
        g = self.gain
        hold_left = self.hold_left
        att, rel, floor = self.att, self.rel, self.range_gain
        opened = False
        for i in range(len(env)):
            if env[i] > threshold:
                hold_left = self.hold_samples
            elif hold_left > 0:
                hold_left -= 1
            target = 1.0 if (env[i] > threshold or hold_left > 0) else floor
            if target == 1.0:
                opened = True
            coef = att if target > g else rel
            g = coef * g + (1.0 - coef) * target
            gains[i] = g
        self.gain = g
        self.hold_left = hold_left
        self.open = opened
        return (x * gains[:, None]).astype(np.float32)


class DspChain:
    """信号链：HPF/LPF → Gate → Compressor → 3 段 EQ。

    控制线程调 set_params（只写 pending，不等待），音频线程每块开头取一次。
    总开关与单模块开关都做块内淡入淡出（12 ms），避免旁通/预设切换时爆音。
    """

    FADE_MS = 12.0

    def __init__(self, rate: int = 48000, channels: int = 2):
        self.rate = rate
        self.channels = channels
        self.filters = HiLoCut(rate, channels)
        self.gate = Gate(rate)
        self.comp = Compressor(rate)
        self.eq = ThreeBandEQ(rate, channels)
        self.enabled = False
        self._pending: dict | None = None
        self._lock = threading.Lock()
        self.nan_blocks = 0
        self._mix = 0.0          # 0 = 干声，1 = 全湿声
        self._ghost: dict[str, object] = {}  # 开关切换时的旧状态快照

    def set_params(self, params: dict):
        with self._lock:
            self._pending = params

    def _apply_pending(self):
        pending = self._pending
        if pending is None:
            return
        self._pending = None
        hp = pending.get("hpf", {})
        lp = pending.get("lpf", {})
        gate = pending.get("gate", {})
        comp = pending.get("comp", {})
        eq = pending.get("eq", {})

        # 开关状态发生变化时，把旧配置连同内部状态拍一份快照，
        # 下一块用「旧信号 → 新信号」的交叉淡化过渡，避免阶跃爆音。
        wanted = {
            "filters": bool(hp.get("enabled")) or bool(lp.get("enabled")),
            "gate": bool(gate.get("enabled")),
            "comp": bool(comp.get("enabled")),
            "eq": bool(eq.get("enabled")),
        }
        current = {
            "filters": self.filters.hp_enabled or self.filters.lp_enabled,
            "gate": self.gate.enabled,
            "comp": self.comp.enabled,
            "eq": self.eq.enabled,
        }
        for key, stage in (("filters", self.filters), ("gate", self.gate),
                           ("comp", self.comp), ("eq", self.eq)):
            if bool(wanted[key]) != bool(current[key]):
                try:
                    self._ghost[key] = copy.deepcopy(stage)
                except Exception:
                    self._ghost.pop(key, None)

        self.enabled = bool(pending.get("enabled"))
        self.filters.configure(hp, lp)
        self.gate.configure(gate)
        self.comp.configure(comp)
        self.eq.configure(eq)

    def _run_stage(self, key: str, stage, x: np.ndarray) -> np.ndarray:
        wet = stage.process(x)
        ghost = self._ghost.pop(key, None)
        if ghost is not None and len(x):
            old = ghost.process(x)
            w = np.linspace(0.0, 1.0, len(x), dtype=np.float64)[:, None]
            return (old * (1.0 - w) + wet * w).astype(np.float32)
        return wet

    def process(self, x: np.ndarray) -> np.ndarray:
        # 音频线程：拿不到锁就沿用上一块参数，绝不等待。
        if self._lock.acquire(blocking=False):
            try:
                self._apply_pending()
            finally:
                self._lock.release()

        target = 1.0 if self.enabled else 0.0
        if target == 0.0 and self._mix <= 1e-4:
            self._mix = 0.0
            return x
        if not len(x):
            return x

        try:
            y = self._run_stage("filters", self.filters, x)
            y = self._run_stage("gate", self.gate, y)
            y = self._run_stage("comp", self.comp, y)
            y = self._run_stage("eq", self.eq, y)

            # 总开关：整链湿声与原始干声在 12 ms 内交叉淡化。
            if self._mix != target:
                delta = len(x) / max(1.0, self.FADE_MS / 1000.0 * self.rate)
                curve = np.clip(self._mix + delta * np.linspace(0.0, 1.0, len(x)), 0.0, 1.0)
                self._mix = float(curve[-1])
                w = curve[:, None].astype(np.float64)
                y = (x * (1.0 - w) + y * w).astype(np.float32)

            if not np.isfinite(y).all():
                self.nan_blocks += 1
                return x
            return y
        except Exception:
            # 实时路径不允许把异常抛进声卡回调。
            self.nan_blocks += 1
            return x

    def metrics(self) -> dict:
        # 参数刚下发、音频线程还没跑到时，以 pending 为准，避免 UI 显示回退。
        pending = self._pending
        enabled = bool(pending["enabled"]) if isinstance(pending, dict) else self.enabled
        return {
            "enabled": enabled,
            "pending": pending is not None,
            "grDb": round(self.comp.gr_db, 2),
            "gateOpen": bool(self.gate.open),
            "nanBlocks": self.nan_blocks,
        }
