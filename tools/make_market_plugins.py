"""生成插件市场的创意工坊插件（plugin-market/plugins/*），并重建 catalog.json。

用法：
    .venv\\Scripts\\python.exe tools\\make_market_plugins.py            # 生成 + 校验 + 写入
    .venv\\Scripts\\python.exe tools\\make_market_plugins.py --no-install   # 不只装到本机工坊

每个插件都是 workshop 格式：JSON 清单 + 一段只依赖 numpy 的 process(x, sr, params)。
生成后会逐个编译并用立体声测试块跑一遍，形状/NaN/静音都会立即报错。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

import workshop_service  # noqa: E402

MARKET = ROOT / "plugin-market"
PLUGINS_DIR = MARKET / "plugins"
CATALOG = MARKET / "catalog.json"
# 国内主源：Gitee 仓库（catalog 里的 manifest / homepage 都指向这里）。
RAW = "https://gitee.com/handa2500/intro-voice-plugin-market/raw/main/plugins"
TREE = "https://gitee.com/handa2500/intro-voice-plugin-market/tree/main/plugins"

# ---------------------------------------------------------------------------
# 可复用的代码片段（插件脚本里不能 import，只能依赖 np / 内置小工具）
# ---------------------------------------------------------------------------
FILTERS = '''
_LP_HIST = {}


def _lowpass(x, sr, hz):
    hz = max(20.0, min(sr * 0.45, hz))
    a = np.exp(-2.0 * np.pi * hz / sr)
    taps = int(min(256, max(8, sr / hz * 2)))
    k = (1.0 - a) * np.power(a, np.arange(taps))
    k = k / np.sum(k)
    ch = x.shape[1]
    key = round(hz, 1)
    if len(_LP_HIST) > 32:
        _LP_HIST.clear()
    state = _LP_HIST.get(key)
    if state is None or state.shape[1] != ch:
        state = np.zeros((taps - 1, ch), np.float32)
    # 带着上一块的尾巴一起卷积：块边界不会重新起振，否则每 40 ms 一次爆音。
    padded = np.vstack([state, x])
    out = np.empty_like(x)
    for c in range(ch):
        out[:, c] = np.convolve(padded[:, c], k, mode="valid")[:len(x)]
    _LP_HIST[key] = padded[-(taps - 1):]
    return out


def _highpass(x, sr, hz):
    return x - _lowpass(x, sr, hz)


def _bandpass(x, sr, lo, hi):
    return _highpass(_lowpass(x, sr, hi), sr, lo)
'''

PITCH = '''
_SIZE = 1 << 15
_GRAIN = 2048.0
_PBUF = None
_PW = 0
_PPH = 0.0


def _shift(x, sr, semi, mix=1.0):
    # 双读头颗粒移调：两个读头相差半个颗粒、余弦平方交叉淡化，避免爆音。
    global _PBUF, _PW, _PPH
    n, ch = x.shape
    if _PBUF is None or _PBUF.shape[1] != ch:
        _PBUF = np.zeros((_SIZE, ch), np.float32)
        _PW = 0
        _PPH = 0.0
    ratio = 2.0 ** (semi / 12.0)
    idx = (_PW + np.arange(n)) % _SIZE
    _PBUF[idx] = x
    pos = np.arange(n, dtype=np.float64)
    phase = (_PPH + pos * (1.0 - ratio)) % _GRAIN
    d1 = phase
    d2 = (phase + _GRAIN * 0.5) % _GRAIN
    # 权重必须在读头回绕处为 0，否则每颗粒一次「跳回现在」会变成明显爆音（听感就是一卡一卡）。
    w1 = np.square(np.sin(np.pi * d1 / _GRAIN))
    w2 = 1.0 - w1

    def read(delay):
        at = (_PW + pos - delay) % _SIZE
        i0 = np.floor(at).astype(np.int64)
        frac = (at - i0)[:, None]
        return _PBUF[i0] * (1.0 - frac) + _PBUF[(i0 + 1) % _SIZE] * frac

    shifted = read(d1) * w1[:, None] + read(d2) * w2[:, None]
    _PW = (_PW + n) % _SIZE
    _PPH = float((_PPH + n * (1.0 - ratio)) % _GRAIN)
    return (x * (1.0 - mix) + shifted * mix).astype(np.float32)
'''

DELAY = '''
_SIZE = 1 << 18
_DLINES = {}


def _delay(x, sr, ms, feedback, mix, spread=0.0, slot="a"):
    # 每条延迟线独立 slot：一个插件里串联两条 delay 时不会互相踩缓冲区。
    n, ch = x.shape
    line = _DLINES.get(slot)
    if line is None or line[0].shape[1] != ch:
        line = (np.zeros((_SIZE, ch), np.float32), 0)
    buf, w = line
    out = np.empty_like(x)
    frame = (w + np.arange(n)) % _SIZE
    for c in range(ch):
        d = int(sr * (ms + (spread if c else 0.0)) / 1000.0)
        d = max(1, min(_SIZE - n - 1, d))
        at = (frame - d) % _SIZE
        wet = buf[at, c].copy()
        buf[frame, c] = x[:, c] + wet * feedback
        out[:, c] = x[:, c] * (1.0 - mix) + wet * mix
    _DLINES[slot] = (buf, (w + n) % _SIZE)
    return out.astype(np.float32)
'''

MODDELAY = '''
_SIZE = 1 << 17
_MBUF = None
_MW = 0
_MPH = 0.0


def _mod_delay(x, sr, base_ms, depth_ms, rate_hz, mix, fb=0.0):
    global _MBUF, _MW, _MPH
    n, ch = x.shape
    if _MBUF is None or _MBUF.shape[1] != ch:
        _MBUF = np.zeros((_SIZE, ch), np.float32)
        _MW = 0
        _MPH = 0.0
    frame = (_MW + np.arange(n)) % _SIZE
    _MBUF[frame] = x
    t = (_MPH + np.arange(n, dtype=np.float64)) / sr
    sweep = base_ms + depth_ms * np.sin(2.0 * np.pi * rate_hz * t)
    at = (_MW + np.arange(n) - sweep * sr / 1000.0) % _SIZE
    i0 = np.floor(at).astype(np.int64)
    frac = (at - i0)[:, None]
    wet = _MBUF[i0] * (1.0 - frac) + _MBUF[(i0 + 1) % _SIZE] * frac
    _MW = (_MW + n) % _SIZE
    _MPH = float((_MPH + n) % (sr * 30))
    return (x * (1.0 - mix) + wet * mix).astype(np.float32)
'''

CRUSH = '''
_CRUSH_CNT = 0
_CRUSH_HELD = None


def _crush(x, bits, hold):
    # 采样保持要跨块连续：每块重新分相会在块边界产生规律性爆音。
    global _CRUSH_CNT, _CRUSH_HELD
    levels = float(2 ** int(bits))
    hold = max(1, int(hold))
    ch = x.shape[1]
    if _CRUSH_HELD is None or _CRUSH_HELD.shape[0] != ch:
        _CRUSH_HELD = np.zeros(ch, np.float32)
        _CRUSH_CNT = 0
    out = np.empty_like(x)
    held = _CRUSH_HELD
    cnt = _CRUSH_CNT
    for i in range(len(x)):
        if cnt == 0:
            held = x[i].copy()
        out[i] = held
        cnt += 1
        if cnt >= hold:
            cnt = 0
    _CRUSH_HELD = held
    _CRUSH_CNT = cnt
    return np.round(out * levels) / levels
'''

RING = '''
def _ring(x, sr, hz, depth, phase=[0.0]):
    # 相位按整秒回绕：hz 为整数时 cos(2π·hz·(t+1)) 与回绕前完全一致，不会爆音。
    n = len(x)
    t = (np.arange(n, dtype=np.float64) + phase[0]) / sr
    mod = np.cos(2.0 * np.pi * hz * t)[:, None]
    phase[0] = (phase[0] + n) % sr
    return x * (1.0 - depth + depth * mod)
'''

SUB = '''
_SUBPH = 0.0
_SUBENV = 0.0


def _sub(x, sr, hz):
    # 正弦相位跨块连续、包络按块线性过渡，否则每块边界都会“咔”一下。
    global _SUBPH, _SUBENV
    n, ch = x.shape
    target = float(np.mean(np.abs(x)))
    env = _SUBENV + (target - _SUBENV) * (np.arange(1, n + 1, dtype=np.float32) / n)
    _SUBENV = target
    t = (_SUBPH + np.arange(n, dtype=np.float64)) / sr
    tone = np.sin(2.0 * np.pi * hz * t)[:, None]
    _SUBPH = float((_SUBPH + n) % sr)
    return (tone * env[:, None]).astype(np.float32)
'''


def plugin(slug: str, name: str, summary: str, description: str, code: str,
           license_name: str = "MIT") -> dict:
    return {"slug": slug, "name": name, "summary": summary, "description": description,
            "code": code.strip() + "\n", "license": license_name}


PLUGINS: list[dict] = [
    # ---------------- 搞笑变音 ----------------
    plugin("chipmunk", "花栗鼠", "升 7 个半音的花栗鼠音色，直播整活常用。",
           "颗粒移调把声音整体升高 7 个半音，再切掉低频，得到又尖又快的花栗鼠效果。",
           PITCH + FILTERS + '''
PARAMS = [
    {"id": "semi", "name": "音高", "min": 0.0, "max": 12.0, "default": 7.0, "step": 1.0, "unit": "st"},
    {"id": "tone", "name": "高通", "min": 60.0, "max": 400.0, "default": 140.0, "step": 10.0, "unit": "Hz"},
    {"id": "mix", "name": "混合", "min": 0.0, "max": 1.0, "default": 1.0, "step": 0.01},
]


def process(x, sr, params):
    y = _shift(x, sr, params["semi"], params["mix"])
    y = _highpass(y, sr, params["tone"])
    return np.clip(y, -1.2, 1.2).astype(np.float32)
'''),
    plugin("deep-monster", "低沉怪兽", "降调 + 低通 + 失真，变成低沉的大怪兽。",
           "向下移调 6 个半音，再叠一层低通与轻度失真，适合恐怖/搞怪配音。",
           PITCH + FILTERS + '''
PARAMS = [
    {"id": "semi", "name": "音高", "min": -12.0, "max": 0.0, "default": -6.0, "step": 1.0, "unit": "st"},
    {"id": "tone", "name": "低通", "min": 800.0, "max": 8000.0, "default": 3000.0, "step": 100.0, "unit": "Hz"},
    {"id": "drive", "name": "失真", "min": 0.5, "max": 6.0, "default": 2.2, "step": 0.1},
    {"id": "mix", "name": "混合", "min": 0.0, "max": 1.0, "default": 1.0, "step": 0.01},
]


def process(x, sr, params):
    y = _shift(x, sr, params["semi"], params["mix"])
    y = np.tanh(y * params["drive"]) / np.tanh(params["drive"])
    y = _lowpass(y, sr, params["tone"])
    return np.clip(y, -1.0, 1.0).astype(np.float32)
'''),
    plugin("helium", "氦气球", "升高 10 个半音的氦气嗓音，带高频提亮。",
           "夸张的升调 + 高通 + 高频搁架，一秒变氦气球主持人。",
           PITCH + FILTERS + '''
PARAMS = [
    {"id": "semi", "name": "音高", "min": 6.0, "max": 14.0, "default": 10.0, "step": 1.0, "unit": "st"},
    {"id": "tone", "name": "高通", "min": 100.0, "max": 500.0, "default": 220.0, "step": 10.0, "unit": "Hz"},
    {"id": "sparkle", "name": "提亮", "min": 0.0, "max": 1.0, "default": 0.5, "step": 0.01},
]


def process(x, sr, params):
    y = _highpass(_shift(x, sr, params["semi"]), sr, params["tone"])
    high = _highpass(y, sr, 4000.0)
    y = y + high * params["sparkle"] * 1.5
    return np.clip(y, -1.2, 1.2).astype(np.float32)
'''),
    plugin("alien-radio", "外星人电台", "移调 + 环形调制 + 颤音，像外星人在广播。",
           "轻微升调后加环形调制与缓慢颤音，营造外星电台的金属波动感。",
           PITCH + RING + FILTERS + '''
PARAMS = [
    {"id": "semi", "name": "音高", "min": -3.0, "max": 6.0, "default": 3.0, "step": 1.0, "unit": "st"},
    {"id": "ring", "name": "环形调制", "min": 20.0, "max": 160.0, "default": 55.0, "step": 1.0, "unit": "Hz"},
    {"id": "depth", "name": "调制深度", "min": 0.0, "max": 1.0, "default": 0.45, "step": 0.01},
    {"id": "mix", "name": "混合", "min": 0.0, "max": 1.0, "default": 0.9, "step": 0.01},
]


def process(x, sr, params):
    y = _shift(x, sr, params["semi"], 1.0)
    y = _ring(y, sr, params["ring"], params["depth"])
    y = _bandpass(y, sr, 180.0, 6500.0)
    return ((1.0 - params["mix"]) * x + params["mix"] * y).astype(np.float32)
'''),
    plugin("robot-voice", "机器人", "环形调制 + 梳状滤波，机械感十足。",
           "用深度环形调制与短梳状延迟做出金属机器人声线，适合整活与旁白。",
           RING + FILTERS + '''
_RBUF = None
_RW = 0

PARAMS = [
    {"id": "ring", "name": "环调频率", "min": 40.0, "max": 300.0, "default": 90.0, "step": 5.0, "unit": "Hz"},
    {"id": "depth", "name": "调制深度", "min": 0.0, "max": 1.0, "default": 0.8, "step": 0.01},
    {"id": "drive", "name": "硬度", "min": 1.0, "max": 4.0, "default": 1.6, "step": 0.1},
]


def process(x, sr, params):
    global _RBUF, _RW
    n, ch = x.shape
    size = max(4096, int(sr * 0.12))
    if _RBUF is None or _RBUF.shape[0] != size or _RBUF.shape[1] != ch:
        _RBUF = np.zeros((size, ch), np.float32)
        _RW = 0
    ring = _ring(x, sr, params["ring"], params["depth"])
    comb_ms = 1000.0 / max(params["ring"], 20.0)
    d = max(1, min(size - n - 1, int(sr * comb_ms / 2000.0)))
    at = (_RW + np.arange(n) - d) % size
    dry = _RBUF[at].copy()
    widx = (_RW + np.arange(n)) % size
    _RBUF[widx] = ring + dry * 0.55
    _RW = (_RW + n) % size
    y = ring + dry * 0.5
    y = np.tanh(y * params["drive"]) / np.tanh(params["drive"])
    return np.clip(y, -1.0, 1.0).astype(np.float32)
'''),
    plugin("droid-metallic", "金属机器人", "高频环形调制 + 高通，冷硬的机器人腔。",
           "比「机器人」更亮更硬：高环调频率 + 高通 + 削波，适合科幻机器角色。",
           RING + FILTERS + '''
PARAMS = [
    {"id": "ring", "name": "环调频率", "min": 150.0, "max": 600.0, "default": 260.0, "step": 10.0, "unit": "Hz"},
    {"id": "depth", "name": "深度", "min": 0.2, "max": 1.0, "default": 0.8, "step": 0.01},
    {"id": "tone", "name": "高通", "min": 200.0, "max": 1200.0, "default": 500.0, "step": 20.0, "unit": "Hz"},
    {"id": "drive", "name": "硬度", "min": 1.0, "max": 4.0, "default": 1.8, "step": 0.1},
]


def process(x, sr, params):
    y = _ring(x, sr, params["ring"], params["depth"])
    y = _highpass(y, sr, params["tone"])
    y = np.tanh(y * params["drive"]) / np.tanh(params["drive"])
    return np.clip(y, -1.0, 1.0).astype(np.float32)
'''),
    plugin("telephone", "电话听筒", "300–3400 Hz 带通 + 轻失真，经典电话音。",
           "把声音限制在电话频带内并加一点底噪，适合整活、广播剧与回忆杀。",
           FILTERS + '''
PARAMS = [
    {"id": "lo", "name": "低切", "min": 200.0, "max": 600.0, "default": 300.0, "step": 10.0, "unit": "Hz"},
    {"id": "hi", "name": "高切", "min": 2000.0, "max": 4500.0, "default": 3400.0, "step": 50.0, "unit": "Hz"},
    {"id": "drive", "name": "失真", "min": 0.5, "max": 3.0, "default": 1.4, "step": 0.1},
    {"id": "noise", "name": "底噪", "min": 0.0, "max": 0.02, "default": 0.004, "step": 0.001},
]


def process(x, sr, params):
    y = _bandpass(x, sr, params["lo"], params["hi"])
    y = np.tanh(y * params["drive"]) / np.tanh(params["drive"])
    y = y + (np.random.rand(*y.shape).astype(np.float32) - 0.5) * params["noise"]
    return np.clip(y, -1.0, 1.0).astype(np.float32)
'''),
    plugin("walkie-talkie", "对讲机", "窄带 + 压缩 + 沙沙声，喊话立即上线。",
           "极窄频带与硬压缩模拟对讲机，配一点底噪，适合「收到请回答」式整活。",
           FILTERS + '''
PARAMS = [
    {"id": "lo", "name": "低切", "min": 300.0, "max": 900.0, "default": 500.0, "step": 10.0, "unit": "Hz"},
    {"id": "hi", "name": "高切", "min": 1800.0, "max": 3500.0, "default": 2500.0, "step": 50.0, "unit": "Hz"},
    {"id": "drive", "name": "过载", "min": 1.0, "max": 4.0, "default": 2.2, "step": 0.1},
    {"id": "noise", "name": "底噪", "min": 0.0, "max": 0.03, "default": 0.008, "step": 0.001},
]


def process(x, sr, params):
    y = _bandpass(x, sr, params["lo"], params["hi"])
    y = np.tanh(y * params["drive"]) / np.tanh(params["drive"])
    level = np.sqrt(np.mean(np.square(y)) + 1e-9)
    if level > 0.02:
        y = y * (0.14 / level) * 0.55
    y = y + (np.random.rand(*y.shape).astype(np.float32) - 0.5) * params["noise"]
    return np.clip(y, -1.0, 1.0).astype(np.float32)
'''),
    plugin("old-radio", "老式收音机", "窄带 + 轻失真 + 沙沙噪，复古广播味。",
           "模拟 AM 收音机的频带与底噪，再补一点噼啪声，适合怀旧场景。",
           FILTERS + '''
PARAMS = [
    {"id": "lo", "name": "低切", "min": 400.0, "max": 1200.0, "default": 600.0, "step": 20.0, "unit": "Hz"},
    {"id": "hi", "name": "高切", "min": 2200.0, "max": 5000.0, "default": 3000.0, "step": 50.0, "unit": "Hz"},
    {"id": "drive", "name": "失真", "min": 0.5, "max": 3.0, "default": 1.3, "step": 0.1},
    {"id": "crackle", "name": "噼啪", "min": 0.0, "max": 0.03, "default": 0.006, "step": 0.001},
]


def process(x, sr, params):
    y = _bandpass(x, sr, params["lo"], params["hi"])
    y = np.tanh(y * params["drive"]) / np.tanh(params["drive"])
    crackle = np.random.rand(*y.shape).astype(np.float32)
    crackle = (crackle > 0.9995).astype(np.float32) * (np.random.rand(*y.shape) - 0.5)
    y = y + crackle * params["crackle"] * 8.0
    return np.clip(y, -1.0, 1.0).astype(np.float32)
'''),
    plugin("underwater", "水下发闷", "低通 + 缓慢摆动，像在水里说话。",
           "重低通配合 0.5 Hz 的摆动，营造水下咕噜咕噜的对话感。",
           '''
_PH = 0.0
_UW_STATE = None

PARAMS = [
    {"id": "cutoff", "name": "低通", "min": 300.0, "max": 1500.0, "default": 700.0, "step": 20.0, "unit": "Hz"},
    {"id": "wobble", "name": "摆动", "min": 0.0, "max": 1.0, "default": 0.5, "step": 0.01},
    {"id": "speed", "name": "摆动速度", "min": 0.1, "max": 2.0, "default": 0.45, "step": 0.05, "unit": "Hz"},
]


def process(x, sr, params):
    # 截止频率逐样本变化：用一极点滤波并保留状态，避免分段 FIR 在段边界爆音。
    global _PH, _UW_STATE
    n, ch = x.shape
    t = (np.arange(n, dtype=np.float64) + _PH) / sr
    wobble = 1.0 + 0.35 * params["wobble"] * np.sin(2.0 * np.pi * params["speed"] * t)
    cutoff = np.clip(params["cutoff"] * wobble, 80.0, 3000.0)
    coef = np.exp(-2.0 * np.pi * cutoff / sr)
    if _UW_STATE is None or _UW_STATE.shape[0] != ch:
        _UW_STATE = np.zeros(ch, np.float32)
    state = _UW_STATE
    y = np.empty_like(x)
    for i in range(n):
        a = coef[i]
        state = a * state + (1.0 - a) * x[i]
        y[i] = state
    _UW_STATE = state
    _PH = float((_PH + n) % (sr * 8))
    return y.astype(np.float32)
'''),
    plugin("cave-echo", "山洞回声", "多抽头延迟 + 反馈，空荡山洞感。",
           "三组不等间隔的延迟叠出山洞回声，反馈越高回响越长。",
           DELAY + '''
PARAMS = [
    {"id": "time", "name": "回声间隔", "min": 40.0, "max": 220.0, "default": 95.0, "step": 1.0, "unit": "ms"},
    {"id": "feedback", "name": "反馈", "min": 0.0, "max": 0.85, "default": 0.5, "step": 0.01},
    {"id": "mix", "name": "湿度", "min": 0.0, "max": 1.0, "default": 0.45, "step": 0.01},
]


def process(x, sr, params):
    a = _delay(x, sr, params["time"], params["feedback"], params["mix"], slot="a")
    b = _delay(x, sr, params["time"] * 1.7, params["feedback"] * 0.7, params["mix"] * 0.6, slot="b")
    return np.clip((a + b) * 0.6, -1.2, 1.2).astype(np.float32)
'''),
    plugin("stadium", "体育场广播", "中频突出 + 混响，场馆播报感。",
           "压缩中频、加长混响并收掉低频，适合比赛播报、开场介绍。",
           FILTERS + DELAY + '''
PARAMS = [
    {"id": "verb", "name": "混响", "min": 0.0, "max": 1.0, "default": 0.35, "step": 0.01},
    {"id": "drive", "name": "响度", "min": 1.0, "max": 4.0, "default": 1.8, "step": 0.1},
    {"id": "presence", "name": "中频", "min": 0.0, "max": 1.0, "default": 0.5, "step": 0.01},
]


def process(x, sr, params):
    y = _bandpass(x, sr, 180.0, 6500.0)
    y = y + _bandpass(y, sr, 1200.0, 3200.0) * params["presence"]
    y = np.tanh(y * params["drive"]) / np.tanh(params["drive"])
    wet = _delay(y, sr, 55.0, 0.55, params["verb"] * 0.5, slot="a")
    wet = _delay(wet, sr, 130.0, 0.45, params["verb"] * 0.35, slot="b")
    return np.clip(y * (1.0 - params["verb"] * 0.4) + wet, -1.2, 1.2).astype(np.float32)
'''),
    # ---------------- 鬼畜 / 卡通 ----------------
    plugin("slow-demon", "鬼畜慢放", "降 8 个半音 + 混响，慢速魔王低语。",
           "向下移调叠加空洞混响，做成鬼畜视频里的慢放魔王音。",
           PITCH + DELAY + '''
PARAMS = [
    {"id": "semi", "name": "音高", "min": -12.0, "max": -4.0, "default": -8.0, "step": 1.0, "unit": "st"},
    {"id": "verb", "name": "混响", "min": 0.0, "max": 1.0, "default": 0.45, "step": 0.01},
    {"id": "mix", "name": "混合", "min": 0.0, "max": 1.0, "default": 1.0, "step": 0.01},
]


def process(x, sr, params):
    y = _shift(x, sr, params["semi"], params["mix"])
    wet = _delay(y, sr, 160.0, 0.55, params["verb"])
    return np.clip(y * (1.0 - params["verb"] * 0.35) + wet, -1.2, 1.2).astype(np.float32)
'''),
    plugin("fast-host", "快嘴主播", "升 5 个半音 + 压缩，语速快一倍的主播腔。",
           "升调配合压缩与高频提亮，得到又急又亮的主播嗓音。",
           PITCH + FILTERS + '''
PARAMS = [
    {"id": "semi", "name": "音高", "min": 2.0, "max": 8.0, "default": 4.0, "step": 1.0, "unit": "st"},
    {"id": "tone", "name": "高通", "min": 80.0, "max": 400.0, "default": 160.0, "step": 10.0, "unit": "Hz"},
    {"id": "drive", "name": "压缩", "min": 1.0, "max": 4.0, "default": 2.0, "step": 0.1},
]


def process(x, sr, params):
    y = _highpass(_shift(x, sr, params["semi"]), sr, params["tone"])
    y = np.tanh(y * params["drive"]) / np.tanh(params["drive"]) * 0.9
    high = _highpass(y, sr, 5000.0)
    return np.clip(y + high * 0.4, -1.2, 1.2).astype(np.float32)
'''),
    plugin("anime-cute", "二次元萌音", "升 4 个半音 + 高频闪光，甜美配音感。",
           "轻度升调与高频提亮，做出动画角色的甜美元气音色。",
           PITCH + FILTERS + '''
PARAMS = [
    {"id": "semi", "name": "音高", "min": 2.0, "max": 8.0, "default": 4.0, "step": 1.0, "unit": "st"},
    {"id": "sparkle", "name": "高频", "min": 0.0, "max": 1.0, "default": 0.5, "step": 0.01},
    {"id": "tone", "name": "高通", "min": 80.0, "max": 400.0, "default": 180.0, "step": 10.0, "unit": "Hz"},
]


def process(x, sr, params):
    y = _highpass(_shift(x, sr, params["semi"]), sr, params["tone"])
    high = _highpass(y, sr, 5000.0)
    y = y + high * params["sparkle"] * 1.2
    return np.clip(y * 0.95, -1.2, 1.2).astype(np.float32)
'''),
    plugin("whisper", "气声耳语", "高通 + 噪声混合，变成悄悄话。",
           "切掉低频主体、混入气声噪声，做出贴耳低语的氛围。",
           FILTERS + '''
PARAMS = [
    {"id": "tone", "name": "高通", "min": 800.0, "max": 3000.0, "default": 1500.0, "step": 50.0, "unit": "Hz"},
    {"id": "noise", "name": "气声", "min": 0.0, "max": 0.15, "default": 0.05, "step": 0.005},
    {"id": "mix", "name": "混合", "min": 0.2, "max": 1.0, "default": 0.85, "step": 0.01},
]


def process(x, sr, params):
    y = _highpass(x, sr, params["tone"])
    air = np.random.rand(*y.shape).astype(np.float32) - 0.5
    air = _highpass(air, sr, params["tone"])
    y = y + air * params["noise"] * 4.0
    return ((1.0 - params["mix"]) * x + params["mix"] * y).astype(np.float32)
'''),
    plugin("villain", "大魔王", "降调 + 低频 + 失真 + 混响，反派登场。",
           "降 4 个半音、补强低频、加失真与长混响，适合反派台词。",
           PITCH + FILTERS + DELAY + SUB + '''
PARAMS = [
    {"id": "semi", "name": "音高", "min": -8.0, "max": -2.0, "default": -4.0, "step": 1.0, "unit": "st"},
    {"id": "drive", "name": "失真", "min": 1.0, "max": 5.0, "default": 2.4, "step": 0.1},
    {"id": "sub", "name": "低频", "min": 0.0, "max": 1.0, "default": 0.5, "step": 0.01},
    {"id": "verb", "name": "混响", "min": 0.0, "max": 1.0, "default": 0.4, "step": 0.01},
]


def process(x, sr, params):
    y = _shift(x, sr, params["semi"])
    y = np.tanh(y * params["drive"]) / np.tanh(params["drive"])
    y = y + _sub(y, sr, 70.0) * params["sub"] * 0.7
    y = _lowpass(y, sr, 7000.0)
    wet = _delay(y, sr, 140.0, 0.5, params["verb"])
    return np.clip(y * (1.0 - params["verb"] * 0.3) + wet, -1.2, 1.2).astype(np.float32)
'''),
    plugin("chipmunk-choir", "花栗鼠合唱团", "升调 + 失谐双声部，一群花栗鼠。",
           "升 7 个半音后再叠一层失谐短延迟，像一群花栗鼠同时说话。",
           PITCH + DELAY + FILTERS + '''
PARAMS = [
    {"id": "semi", "name": "音高", "min": 4.0, "max": 10.0, "default": 7.0, "step": 1.0, "unit": "st"},
    {"id": "detune", "name": "失谐", "min": 0.0, "max": 1.0, "default": 0.5, "step": 0.01},
    {"id": "mix", "name": "混合", "min": 0.0, "max": 1.0, "default": 1.0, "step": 0.01},
]


def process(x, sr, params):
    y = _shift(x, sr, params["semi"], params["mix"])
    double = _delay(y, sr, 18.0 * (0.4 + params["detune"]), 0.0, params["detune"] * 0.5, 7.0)
    y = _highpass(y + double, sr, 120.0)
    return np.clip(y * 0.9, -1.2, 1.2).astype(np.float32)
'''),
    plugin("8bit-pixel", "8-bit 像素音", "比特压缩 + 采样保持，红白机音色。",
           "降低位深并保持采样，做出复古游戏与像素视频的电子音。",
           CRUSH + '''
PARAMS = [
    {"id": "bits", "name": "位深", "min": 4.0, "max": 12.0, "default": 6.0, "step": 1.0, "unit": "bit"},
    {"id": "hold", "name": "采样保持", "min": 2.0, "max": 40.0, "default": 8.0, "step": 1.0},
    {"id": "mix", "name": "混合", "min": 0.0, "max": 1.0, "default": 0.85, "step": 0.01},
]


def process(x, sr, params):
    y = _crush(x, params["bits"], int(params["hold"]))
    return ((1.0 - params["mix"]) * x + params["mix"] * y).astype(np.float32)
'''),
    plugin("flanger-fun", "喷气镶边", "LFO 短延迟，喷气式扫频。",
           "经典镶边：2–8 ms 的短延迟被 LFO 扫动，产生喷气式的金属扫频。",
           MODDELAY + '''
PARAMS = [
    {"id": "rate", "name": "速率", "min": 0.05, "max": 2.0, "default": 0.35, "step": 0.01, "unit": "Hz"},
    {"id": "depth", "name": "深度", "min": 0.0, "max": 1.0, "default": 0.6, "step": 0.01},
    {"id": "mix", "name": "混合", "min": 0.0, "max": 1.0, "default": 0.5, "step": 0.01},
]


def process(x, sr, params):
    return _mod_delay(x, sr, 4.0, 3.0 + 3.0 * params["depth"],
                      params["rate"], params["mix"])
'''),
    plugin("chorus-wide", "合唱叠音", "LFO 短延迟，声音变宽变厚。",
           "12–30 ms 的缓慢调制延迟叠在干声上，做出合唱与加宽效果。",
           MODDELAY + '''
PARAMS = [
    {"id": "rate", "name": "速率", "min": 0.1, "max": 2.0, "default": 0.6, "step": 0.01, "unit": "Hz"},
    {"id": "depth", "name": "厚度", "min": 0.0, "max": 1.0, "default": 0.5, "step": 0.01},
    {"id": "mix", "name": "混合", "min": 0.0, "max": 1.0, "default": 0.5, "step": 0.01},
]


def process(x, sr, params):
    wet = _mod_delay(x, sr, 16.0, 6.0 + 8.0 * params["depth"],
                     params["rate"], params["mix"])
    return np.clip(wet, -1.2, 1.2).astype(np.float32)
'''),
    plugin("tape-stop", "磁带急停", "音高周期性下滑，像磁带没电。",
           "音高按锯齿周期从 0 滑到 -12 个半音，模拟磁带急停与鬼畜转场。",
           PITCH + '''
PARAMS = [
    {"id": "rate", "name": "下滑速度", "min": 0.05, "max": 1.0, "default": 0.25, "step": 0.01, "unit": "Hz"},
    {"id": "depth", "name": "下滑幅度", "min": 0.0, "max": 12.0, "default": 12.0, "step": 1.0, "unit": "st"},
]


def process(x, sr, params):
    n = len(x)
    t = np.arange(n, dtype=np.float64) / sr
    cycle = np.mod(t * params["rate"], 1.0)
    semi = -params["depth"] * cycle
    # 每个块用块首的音高近似，块内音高变化在听感上就是“转速下滑”
    avg = float(np.mean(semi))
    return _shift(x, sr, avg, 1.0)
'''),
    plugin("glitch-stutter", "卡带鬼畜", "周期性重复最近一小段，卡带鬼畜。",
           "按设定间隔反复重放最近一小段音频，做出卡带、鬼畜的重复感。",
           '''
_SIZE = 1 << 16
_GBUF = None
_GW = 0
_GPROG = 0

PARAMS = [
    {"id": "chunk", "name": "重复长度", "min": 40.0, "max": 400.0, "default": 140.0, "step": 10.0, "unit": "ms"},
    {"id": "rate", "name": "触发频率", "min": 0.0, "max": 1.0, "default": 0.35, "step": 0.01},
    {"id": "mix", "name": "混合", "min": 0.0, "max": 1.0, "default": 0.9, "step": 0.01},
]


def process(x, sr, params):
    global _GBUF, _GW, _GPROG
    n, ch = x.shape
    if _GBUF is None or _GBUF.shape[1] != ch:
        _GBUF = np.zeros((_SIZE, ch), np.float32)
        _GW = 0
        _GPROG = 0
    idx = (_GW + np.arange(n)) % _SIZE
    _GBUF[idx] = x
    chunk = max(64, int(sr * params["chunk"] / 1000.0))
    repeat_every = max(chunk * 2, int(chunk * (2.5 - params["rate"] * 1.5)))
    out = x.copy()
    if _GPROG < chunk:
        start = (_GW - repeat_every) % _SIZE
        at = (start + np.arange(n)) % _SIZE
        out = _GBUF[at].copy() * params["mix"] + x * (1.0 - params["mix"])
    _GPROG = (_GPROG + n) % repeat_every
    _GW = (_GW + n) % _SIZE
    return np.clip(out, -1.2, 1.2).astype(np.float32)
'''),
    plugin("sub-voice", "电台低音炮", "低频合成 + 低通，男中音变磁性质感。",
           "从声音包络合成 70 Hz 低频层并压低高频，得到厚重的电台男声。",
           SUB + FILTERS + '''
PARAMS = [
    {"id": "sub", "name": "低频量", "min": 0.0, "max": 1.0, "default": 0.4, "step": 0.01},
    {"id": "hpf", "name": "低切", "min": 30.0, "max": 160.0, "default": 70.0, "step": 5.0, "unit": "Hz"},
    {"id": "tone", "name": "高切", "min": 3000.0, "max": 12000.0, "default": 7000.0, "step": 100.0, "unit": "Hz"},
]


def process(x, sr, params):
    y = _highpass(x, sr, params["hpf"])
    y = y + _sub(y, sr, 70.0) * params["sub"]
    y = _lowpass(y, sr, params["tone"])
    return np.clip(y * 1.1, -1.2, 1.2).astype(np.float32)
'''),
    plugin("stereo-width", "立体声加宽", "M/S 加宽 + 低频收紧，声场更开阔。",
           "通过中侧处理拉宽声场，同时把低频收回中间，避免低音发散。",
           FILTERS + '''
PARAMS = [
    {"id": "width", "name": "宽度", "min": 0.0, "max": 2.0, "default": 1.4, "step": 0.05},
    {"id": "mono", "name": "低频居中", "min": 0.0, "max": 1.0, "default": 0.6, "step": 0.01},
    {"id": "tone", "name": "低切", "min": 40.0, "max": 200.0, "default": 90.0, "step": 5.0, "unit": "Hz"},
]


def process(x, sr, params):
    if x.shape[1] < 2:
        return x
    mid = (x[:, 0] + x[:, 1]) * 0.5
    side = (x[:, 0] - x[:, 1]) * 0.5
    low = _lowpass(np.stack([mid, mid], axis=1), sr, 200.0)[:, 0]
    side = side * params["width"]
    mid = mid * (1.0 - params["mono"] * 0.3) + low * params["mono"] * 0.3
    left = mid + side
    right = mid - side
    y = np.stack([left, right], axis=1)
    y = _highpass(y, sr, params["tone"])
    return np.clip(y, -1.2, 1.2).astype(np.float32)
'''),
    plugin("room-reverb", "房间混响", "多抽头反馈延迟，干净的小房间。",
           "三组不等延迟 + 阻尼反馈，快速得到自然的房间空间感。",
           DELAY + FILTERS + '''
PARAMS = [
    {"id": "size", "name": "房间大小", "min": 0.0, "max": 1.0, "default": 0.5, "step": 0.01},
    {"id": "damping", "name": "阻尼", "min": 0.0, "max": 1.0, "default": 0.5, "step": 0.01},
    {"id": "mix", "name": "湿度", "min": 0.0, "max": 1.0, "default": 0.3, "step": 0.01},
]


def process(x, sr, params):
    base = 28.0 + 70.0 * params["size"]
    wet = _delay(x, sr, base, 0.45 - 0.2 * params["damping"], 1.0, slot="a")
    wet = _delay(wet, sr, base * 1.6, 0.4 - 0.2 * params["damping"], 1.0, slot="b")
    wet = _lowpass(wet, sr, 9000.0 - 5000.0 * params["damping"])
    return np.clip(x * (1.0 - params["mix"]) + wet * params["mix"], -1.2, 1.2).astype(np.float32)
'''),
    plugin("throw-delay", "延迟抛音", "单声道抛音延迟，句尾自动回声。",
           "经典抛音延迟：适合句尾喊话，反馈控制回声次数。",
           DELAY + FILTERS + '''
PARAMS = [
    {"id": "time", "name": "延迟", "min": 120.0, "max": 600.0, "default": 300.0, "step": 5.0, "unit": "ms"},
    {"id": "feedback", "name": "反馈", "min": 0.0, "max": 0.7, "default": 0.35, "step": 0.01},
    {"id": "mix", "name": "湿度", "min": 0.0, "max": 1.0, "default": 0.35, "step": 0.01},
]


def process(x, sr, params):
    y = _delay(x, sr, params["time"], params["feedback"], params["mix"], 8.0)
    y = _lowpass(y, sr, 6000.0)
    return np.clip(y, -1.2, 1.2).astype(np.float32)
'''),
    plugin("presence-boost", "临场感", "1.5–5 kHz 抬升，人声更靠前。",
           "只抬中高频的搁架式提升，让人声在混音里更贴耳。",
           FILTERS + '''
PARAMS = [
    {"id": "gain", "name": "提升", "min": 0.0, "max": 12.0, "default": 4.0, "step": 0.5, "unit": "dB"},
    {"id": "freq", "name": "中心频率", "min": 1500.0, "max": 5000.0, "default": 3000.0, "step": 50.0, "unit": "Hz"},
]


def process(x, sr, params):
    band = _bandpass(x, sr, params["freq"] * 0.6, params["freq"] * 1.6)
    y = x + band * (10.0 ** (params["gain"] / 20.0) - 1.0)
    return np.clip(y, -1.2, 1.2).astype(np.float32)
'''),
    plugin("de-esser", "齿音抑制", "4–9 kHz 动态衰减，压掉刺耳齿音。",
           "检测高频能量并按比例衰减，专门处理 s / sh 的刺耳齿音。",
           FILTERS + '''
PARAMS = [
    {"id": "freq", "name": "中心", "min": 4000.0, "max": 9000.0, "default": 6500.0, "step": 100.0, "unit": "Hz"},
    {"id": "amount", "name": "抑制量", "min": 0.0, "max": 1.0, "default": 0.5, "step": 0.01},
]


def process(x, sr, params):
    band = _bandpass(x, sr, params["freq"] * 0.75, min(sr * 0.45, params["freq"] * 1.5))
    level = np.sqrt(np.mean(np.square(band)) + 1e-12)
    gain = 1.0
    if level > 0.02:
        gain = max(0.15, 0.02 / level) ** params["amount"]
    return np.clip(x - band * (1.0 - gain), -1.2, 1.2).astype(np.float32)
'''),
    plugin("noise-gate", "噪声门", "低于门限静音，去掉底噪与键盘声。",
           "按块检测电平，低于门限时快速衰减，适合键盘声/风扇声较大的环境。",
           '''
_GAIN = 1.0


PARAMS = [
    {"id": "threshold", "name": "门限", "min": -70.0, "max": -30.0, "default": -50.0, "step": 1.0, "unit": "dB"},
    {"id": "range", "name": "衰减", "min": 0.0, "max": 0.9, "default": 0.85, "step": 0.01},
]


def process(x, sr, params):
    global _GAIN
    level = 20.0 * np.log10(np.sqrt(np.mean(np.square(x)) + 1e-12) + 1e-9)
    target = 1.0 if level > params["threshold"] else 1.0 - params["range"]
    n = len(x)
    tau = 0.02 if target > _GAIN else 0.08
    curve = target + (_GAIN - target) * np.exp(
        -np.arange(1, n + 1, dtype=np.float32) / (sr * tau))
    _GAIN = float(curve[-1])
    return (x * curve[:, None]).astype(np.float32)
'''),
]


def validate_and_write(manifest: dict) -> dict:
    compiled = workshop_service.compile_manifest(dict(manifest))
    if compiled.fn is None:
        raise RuntimeError(f"{manifest['slug']} 编译失败：{compiled.error}")
    params = workshop_service.normalize_params(compiled.params, {})
    rng = np.random.default_rng(7)
    block = (rng.standard_normal((1920, 2)) * 0.15).astype(np.float32)
    for _ in range(3):
        out = np.asarray(compiled.fn(block.copy(), 48000, dict(params)), np.float32)
        if out.shape != block.shape:
            raise RuntimeError(f"{manifest['slug']} 输出形状 {out.shape} != {block.shape}")
        if not np.isfinite(out).all():
            raise RuntimeError(f"{manifest['slug']} 输出包含 NaN/Inf")
    return {"params": compiled.params, "peak": float(np.abs(out).max())}


def write_plugin_files(item: dict, params: list[dict]) -> dict:
    slug = item["slug"]
    target = PLUGINS_DIR / slug
    target.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": 1,
        "slug": slug,
        "name": item["name"],
        "author": "Intro Voice",
        "version": "1.0.0",
        "license": item["license"],
        "description": item["description"],
        "code": item["code"],
        "updatedAt": "2026-11-12 00:30:00",
    }
    (target / "plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# {item['name']}", "", item["description"], "", "## 参数", ""]
    for p in params:
        unit = f" {p['unit']}" if p["unit"] else ""
        lines.append(f"- **{p['name']}**（`{p['id']}`）：{p['min']:g} ~ {p['max']:g}{unit}，"
                     f"默认 {p['default']:g}")
    lines += ["", "## 用法", "",
              "在 Intro Voice →「插件市场」中安装，随后到「创意工坊」启用；",
              "滑块可实时调整，所有处理都在音频线程内完成。", ""]
    (target / "README.md").write_text("\n".join(lines), encoding="utf-8")
    return manifest


def rebuild_catalog(new_entries: list[dict]) -> dict:
    try:
        catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        catalog = {"schema": 1, "plugins": []}
    existing = {e.get("slug"): e for e in catalog.get("plugins") or [] if isinstance(e, dict)}
    for entry in new_entries:
        existing[entry["slug"]] = entry
    order = [e.get("slug") for e in catalog.get("plugins") or [] if isinstance(e, dict)]
    ordered: list[dict] = []
    seen: set[str] = set()
    for slug in order:
        if slug in existing and slug not in seen:
            ordered.append(existing[slug])
            seen.add(slug)
    for entry in new_entries:
        if entry["slug"] not in seen:
            ordered.append(existing[entry["slug"]])
            seen.add(entry["slug"])
    catalog["schema"] = 1
    catalog["updatedAt"] = "2026-11-12"
    catalog["plugins"] = ordered
    CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
    return catalog


def install_local(manifests: list[dict]) -> int:
    installed = 0
    for manifest in manifests:
        try:
            workshop_service.save_plugin(ROOT, dict(manifest))
            installed += 1
        except Exception as exc:  # 本地已满或已存在时跳过，不影响市场文件
            print(f"  跳过本机安装 {manifest['slug']}：{exc}")
    return installed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-install", action="store_true")
    args = parser.parse_args()
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)

    manifests = []
    entries = []
    print(f"生成 {len(PLUGINS)} 个工坊插件…")
    for item in PLUGINS:
        try:
            result = validate_and_write(item)
            manifest = write_plugin_files(item, result["params"])
        except Exception as exc:
            print(f"  [失败] {item['slug']}: {exc}")
            return 1
        manifests.append(manifest)
        entries.append({
            "slug": item["slug"],
            "name": item["name"],
            "vendor": "Intro Voice",
            "version": "1.0.0",
            "license": item["license"],
            "copyright": "© Intro Voice · MIT",
            "type": "workshop",
            "summary": item["summary"],
            "homepage": f"{TREE}/{item['slug']}",
            "manifest": f"{RAW}/{item['slug']}/plugin.json",
            "sizeMb": 0.6,
        })
        print(f"  {item['slug']:<18} {len(result['params'])} 参数  峰值 {result['peak']:.2f}")

    catalog = rebuild_catalog(entries)
    print(f"catalog.json 现有 {len(catalog['plugins'])} 条")
    if not args.no_install:
        count = install_local(manifests)
        print(f"已安装到本机工坊：{count}/{len(manifests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
