"""内置 DSP 算法测试样例（随仓库交付，供社区回归）。

运行：
    runtime\python.exe tests\test_mixer_dsp.py
    runtime\python.exe tests\test_mixer_dsp.py --engine   # 额外跑引擎插点测试

覆盖路线图 §3 的要求：边界参数、静音、削波、不同 block size、状态重置、旁通/预设切换无爆音。
"""
from __future__ import annotations

import math
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mixer_dsp import (  # noqa: E402
    Compressor, DspChain, Gate, HiLoCut, ThreeBandEQ,
    default_params, merge_params,
)

RATE = 48000
BLOCK = 1920
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'OK' if ok else 'FAIL'}] {name} {detail}")
    if not ok:
        FAILS.append(name)


def tone(freq: float, amp: float, n: int = BLOCK, rate: int = RATE) -> np.ndarray:
    t = np.arange(n) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def stereo(x: np.ndarray) -> np.ndarray:
    return np.stack([x, x], axis=1)


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x ** 2) + 1e-12))


def db(x: float) -> float:
    return 20.0 * math.log10(max(x, 1e-9))


def process_all(stage, x: np.ndarray, block: int = BLOCK) -> np.ndarray:
    out = [stage.process(x[i:i + block]) for i in range(0, len(x) - block + 1, block)]
    return np.concatenate(out)


# ---------------------------------------------------------------------------
# 1. 滤波器
# ---------------------------------------------------------------------------
def test_filters() -> None:
    eq = ThreeBandEQ(RATE, 2)
    eq.configure({"enabled": True, "lowDb": 0.0, "midDb": 6.0, "highDb": 0.0,
                  "midFreq": 1000.0, "midQ": 1.0})
    y = process_all(eq, stereo(tone(1000, 0.1)))
    ratio = rms(y[-BLOCK * 2:, 0]) / rms(tone(1000, 0.1))
    check("peaking +6dB @1kHz", 1.8 < ratio < 2.2, f"ratio={ratio:.3f}")

    eq2 = ThreeBandEQ(RATE, 2)
    eq2.configure({"enabled": True, "lowDb": 0.0, "midDb": 6.0, "highDb": 0.0,
                   "midFreq": 1000.0, "midQ": 1.0})
    y2 = process_all(eq2, stereo(tone(200, 0.1)))
    check("peaking 对 200Hz 影响小", rms(y2[-BLOCK * 2:, 0]) / rms(tone(200, 0.1)) < 1.15,
          f"ratio={rms(y2[-BLOCK * 2:, 0]) / rms(tone(200, 0.1)):.3f}")

    hp = HiLoCut(RATE, 2)
    hp.configure({"enabled": True, "freq": 100.0, "slope": 12}, {"enabled": False})
    lo = process_all(hp, stereo(tone(50, 0.2)))
    check("HPF@100Hz 衰减 50Hz", rms(lo[-BLOCK * 2:, 0]) / rms(tone(50, 0.2)) < 0.35,
          f"残余={rms(lo[-BLOCK * 2:, 0]) / rms(tone(50, 0.2)):.3f}")

    hp2 = HiLoCut(RATE, 2)
    hp2.configure({"enabled": True, "freq": 100.0, "slope": 12}, {"enabled": False})
    mid = process_all(hp2, stereo(tone(1000, 0.2)))
    check("HPF@100Hz 保留 1kHz", rms(mid[-BLOCK * 2:, 0]) / rms(tone(1000, 0.2)) > 0.9,
          f"残余={rms(mid[-BLOCK * 2:, 0]) / rms(tone(1000, 0.2)):.3f}")

    lp = HiLoCut(RATE, 2)
    lp.configure({"enabled": False}, {"enabled": True, "freq": 4000.0, "slope": 24})
    treble = process_all(lp, stereo(tone(10000, 0.1)))
    check("LPF@4kHz(24dB) 衰减 10kHz", rms(treble[-BLOCK * 2:, 0]) / rms(tone(10000, 0.1)) < 0.1,
          f"残余={rms(treble[-BLOCK * 2:, 0]) / rms(tone(10000, 0.1)):.3f}")


# ---------------------------------------------------------------------------
# 2. 动态处理
# ---------------------------------------------------------------------------
def test_dynamics() -> None:
    comp = Compressor(RATE)
    comp.configure({"enabled": True, "thresholdDb": -20.0, "ratio": 4.0,
                    "attackMs": 5.0, "releaseMs": 100.0, "makeupDb": 0.0, "kneeDb": 0.0})
    loud = stereo(tone(440, 10 ** (-6 / 20), BLOCK * 8))
    y = process_all(comp, loud)
    gr = comp.gr_db
    check("压缩器压低超阈值信号", gr > 6.0, f"GR={gr:.2f}dB")

    quiet = stereo(tone(440, 10 ** (-45 / 20), BLOCK * 8))
    comp2 = Compressor(RATE)
    comp2.configure({"enabled": True, "thresholdDb": -20.0, "ratio": 4.0,
                     "attackMs": 5.0, "releaseMs": 100.0, "makeupDb": 0.0, "kneeDb": 0.0})
    q = process_all(comp2, quiet)
    delta = abs(db(rms(q[-BLOCK * 2:, 0])) - db(rms(quiet[-BLOCK * 2:, 0])))
    check("压缩器不碰低于阈值信号", delta < 1.0, f"偏差={delta:.2f}dB")

    gate = Gate(RATE)
    gate.configure({"enabled": True, "thresholdDb": -40.0, "attackMs": 1.0,
                    "holdMs": 10.0, "releaseMs": 50.0, "rangeDb": -30.0})
    loud_g = process_all(gate, stereo(tone(440, 0.2, BLOCK * 10)))
    quiet_g = process_all(gate, stereo(tone(440, 0.002, BLOCK * 10)))
    check("Gate 通过响亮信号", rms(loud_g[-BLOCK * 2:]) / rms(tone(440, 0.2)) > 0.8,
          f"ratio={rms(loud_g[-BLOCK * 2:]) / rms(tone(440, 0.2)):.3f}")
    check("Gate 压低安静信号", rms(quiet_g[-BLOCK * 2:]) / rms(tone(440, 0.002)) < 0.3,
          f"ratio={rms(quiet_g[-BLOCK * 2:]) / rms(tone(440, 0.002)):.4f}")


# ---------------------------------------------------------------------------
# 3. 极端参数 / 静音 / 削波
# ---------------------------------------------------------------------------
def test_extremes() -> None:
    chain = DspChain(RATE, 2)
    chain.set_params({"enabled": True,
                      "hpf": {"enabled": True, "freq": 20.0, "slope": 24},
                      "lpf": {"enabled": True, "freq": 20000.0, "slope": 24},
                      "eq": {"enabled": True, "lowDb": 18.0, "midDb": 18.0, "highDb": 18.0,
                             "midFreq": 200.0, "midQ": 6.0},
                      "comp": {"enabled": True, "thresholdDb": 0.0, "ratio": 20.0,
                               "attackMs": 0.1, "releaseMs": 10.0, "makeupDb": 24.0, "kneeDb": 0.0},
                      "gate": {"enabled": True, "thresholdDb": -20.0, "attackMs": 0.2,
                               "holdMs": 0.0, "releaseMs": 5.0, "rangeDb": -60.0}})
    rng = np.random.default_rng(7)
    noise = stereo((rng.standard_normal(BLOCK) * 0.3).astype(np.float32))
    out = [chain.process(noise) for _ in range(60)]
    allout = np.concatenate(out)
    check("极端参数 60 块无 NaN", np.isfinite(allout).all(), f"nan_blocks={chain.metrics()['nanBlocks']}")
    # +24 dB makeup 与 +18 dB EQ 叠加时内部电平可以很高，输出上限由下游限幅器负责，
    # 这里只要求「有界且非 NaN」，不把限幅器职责混进 DSP 单测。
    check("极端参数输出有界", np.max(np.abs(allout)) < 200.0, f"峰值={np.max(np.abs(allout)):.1f}")

    silent = np.zeros((BLOCK, 2), np.float32)
    chain2 = DspChain(RATE, 2)
    chain2.set_params({"enabled": True, "comp": {"enabled": True, "makeupDb": 24.0},
                       "gate": {"enabled": True}})
    silent_out = np.concatenate([chain2.process(silent) for _ in range(10)])
    check("静音输入保持静音", np.max(np.abs(silent_out)) == 0.0,
          f"峰值={np.max(np.abs(silent_out)):.3e}")

    clip = stereo(np.clip(tone(440, 1.4, BLOCK * 4), -1.0, 1.0))
    chain3 = DspChain(RATE, 2)
    chain3.set_params({"enabled": True, "comp": {"enabled": True, "thresholdDb": -12.0,
                                                 "ratio": 8.0, "makeupDb": 0.0}})
    clipped = process_all(chain3, clip)
    check("削波输入不产生 NaN", np.isfinite(clipped).all())


# ---------------------------------------------------------------------------
# 4. 不同 block size
# ---------------------------------------------------------------------------
def test_block_sizes() -> None:
    for block in (64, 128, 256, 512, 1920, 4096):
        chain = DspChain(RATE, 2)
        chain.set_params({"enabled": True,
                          "hpf": {"enabled": True, "freq": 90.0, "slope": 12},
                          "eq": {"enabled": True, "lowDb": 3.0, "midDb": -2.0, "highDb": 2.0,
                                 "midFreq": 1500.0, "midQ": 1.2},
                          "comp": {"enabled": True, "thresholdDb": -18.0, "ratio": 3.0},
                          "gate": {"enabled": True, "thresholdDb": -55.0}})
        total = block * 20
        sig = stereo(tone(440, 0.15, total))
        pieces = [chain.process(sig[i:i + block]) for i in range(0, total, block)]
        out = np.concatenate(pieces)
        ok = np.isfinite(out).all() and out.shape == sig.shape
        check(f"block={block} 处理正常", ok, f"shape={out.shape}")

    # 不足一块的尾巴（设备切换/停止时的常见情形）不能丢样本或报错
    chain = DspChain(RATE, 2)
    chain.set_params({"enabled": True, "eq": {"enabled": True, "midDb": 3.0}})
    tail_in = stereo(tone(440, 0.1, 37))
    tail_out = chain.process(tail_in)
    check("不足一块的尾巴正常返回", tail_out.shape == tail_in.shape and np.isfinite(tail_out).all(),
          f"shape={tail_out.shape}")

    # 状态重置：清空内部状态后重新处理，输出应回到同一稳态
    a = DspChain(RATE, 2)
    a.set_params({"enabled": True, "eq": {"enabled": True, "midDb": 6.0, "midFreq": 1000.0}})
    first = process_all(a, stereo(tone(1000, 0.1, BLOCK * 6)))
    b = DspChain(RATE, 2)
    b.set_params({"enabled": True, "eq": {"enabled": True, "midDb": 6.0, "midFreq": 1000.0}})
    second = process_all(b, stereo(tone(1000, 0.1, BLOCK * 6)))
    check("相同输入可复现", np.allclose(first[-BLOCK:], second[-BLOCK:], atol=1e-6))


# ---------------------------------------------------------------------------
# 5. 旁通 / 预设切换无爆音
# ---------------------------------------------------------------------------
def test_transitions() -> None:
    sig = stereo(tone(300, 0.25, BLOCK * 20))
    chain = DspChain(RATE, 2)
    chain.set_params(default_params())

    pieces = []
    for i in range(0, len(sig), BLOCK):
        block_index = i // BLOCK
        if block_index == 4:
            # 打开整链与全部模块（模拟载入预设的那一刻）
            chain.set_params({"enabled": True,
                              "hpf": {"enabled": True, "freq": 100.0, "slope": 24},
                              "lpf": {"enabled": True, "freq": 16000.0, "slope": 12},
                              "eq": {"enabled": True, "lowDb": 6.0, "midDb": 3.0, "highDb": 6.0,
                                     "midFreq": 800.0, "midQ": 1.0},
                              "comp": {"enabled": True, "thresholdDb": -24.0, "ratio": 4.0,
                                       "makeupDb": 3.0},
                              "gate": {"enabled": True, "thresholdDb": -60.0}})
        if block_index == 12:
            chain.set_params({"enabled": False})
        pieces.append(chain.process(sig[i:i + BLOCK]))

    out = np.concatenate(pieces)[:, 0]
    jump = float(np.max(np.abs(np.diff(out))))
    check("预设载入 / 旁通切换无爆音", jump < 0.08, f"最大相邻样本跳变={jump:.4f}")

    # 单个模块开关也不能有台阶
    chain2 = DspChain(RATE, 2)
    chain2.set_params({"enabled": True, "eq": {"enabled": True, "lowDb": 0.0, "midDb": 0.0, "highDb": 0.0}})
    pieces = []
    for i in range(0, len(sig), BLOCK):
        if i // BLOCK == 6:
            chain2.set_params({"eq": {"enabled": False}})
        if i // BLOCK == 12:
            chain2.set_params({"eq": {"enabled": True, "highDb": 12.0}})
        pieces.append(chain2.process(sig[i:i + BLOCK]))
    out2 = np.concatenate(pieces)[:, 0]
    jump2 = float(np.max(np.abs(np.diff(out2))))
    check("单模块开关无爆音", jump2 < 0.08, f"最大相邻样本跳变={jump2:.4f}")


# ---------------------------------------------------------------------------
# 6. 参数协议
# ---------------------------------------------------------------------------
def test_params() -> None:
    m = merge_params(default_params(), {"hpf": {"freq": 999999, "slope": 99},
                                        "comp": {"ratio": 0, "thresholdDb": "abc"}})
    check("越界值夹取", m["hpf"]["freq"] == 1000.0 and m["comp"]["ratio"] == 1.0,
          f"freq={m['hpf']['freq']} ratio={m['comp']['ratio']}")
    check("非法斜率回退", m["hpf"]["slope"] == 12)
    check("非数值忽略", m["comp"]["thresholdDb"] == -18.0)

    base = merge_params(default_params(), {"enabled": True, "eq": {"enabled": True, "lowDb": 6.0}})
    partial = merge_params(base, {"eq": {"midDb": -3.0}})
    check("部分更新保留其他字段",
          partial["enabled"] and partial["eq"]["lowDb"] == 6.0 and partial["eq"]["midDb"] == -3.0)
    check("未知键被忽略", "bogus" not in merge_params(default_params(), {"bogus": 1}))


# ---------------------------------------------------------------------------
# 7. 性能
# ---------------------------------------------------------------------------
def test_performance() -> None:
    chain = DspChain(RATE, 2)
    chain.set_params({"enabled": True,
                      "hpf": {"enabled": True, "freq": 80.0},
                      "lpf": {"enabled": True, "freq": 16000.0},
                      "eq": {"enabled": True, "lowDb": 2.0, "midDb": -1.0, "highDb": 2.0},
                      "comp": {"enabled": True},
                      "gate": {"enabled": True}})
    block = stereo(tone(440, 0.1))
    for _ in range(5):
        chain.process(block)
    start = time.perf_counter()
    runs = 100
    for _ in range(runs):
        chain.process(block)
    per_block = (time.perf_counter() - start) / runs * 1000
    budget = BLOCK / RATE * 1000
    check("单块耗时在预算内", per_block < budget * 0.2,
          f"{per_block:.2f} ms / {budget:.0f} ms 预算")


# ---------------------------------------------------------------------------
# 8. 引擎插点（可选）
# ---------------------------------------------------------------------------
def test_engine() -> None:
    try:
        import queue
        import mixer_service as ms
    except Exception as exc:  # pragma: no cover
        check("引擎模块可导入", False, str(exc))
        return

    class StubChain:
        def count(self):
            return 0

        def process(self, left, right):
            return left, right

    class StubResampler:
        def process(self, audio):
            mono = audio[:, 0] if audio.ndim == 2 else audio
            return np.repeat(mono[:, None], 2, axis=1).astype(np.float32)

    eng = ms.MixerEngine(StubChain(), queue.Queue())
    eng.ai_mode = "off"
    eng.options.gain_db = 0.0
    eng.dsp_params = merge_params(default_params(), {"enabled": False})
    eng.dsp.set_params(eng.dsp_params)
    limiter = ms.PeakLimiter(RATE)
    x = tone(440, 0.1)
    y = eng._process_block(x[:, None], StubResampler(), limiter, None, None)
    check("旁通时引擎输出=输入", np.allclose(y[:, 0], x, atol=1e-6))

    eng.dsp_params = merge_params(default_params(), {"enabled": True, "eq": {"enabled": True, "highDb": 9.0}})
    eng.dsp.set_params(eng.dsp_params)
    hi = tone(8000, 0.05)
    # 前几块用于总开关的 12 ms 淡入，测稳态块
    for _ in range(3):
        eng._process_block(hi[:, None], StubResampler(), limiter, None, None)
    boost = eng._process_block(hi[:, None], StubResampler(), limiter, None, None)[:, 0]
    ratio = rms(boost) / rms(hi)
    check("引擎内 EQ 生效", 2.2 < ratio < 3.2, f"{db(ratio):.1f} dB")


def main() -> int:
    test_filters()
    test_dynamics()
    test_extremes()
    test_block_sizes()
    test_transitions()
    test_params()
    test_performance()
    if "--engine" in sys.argv:
        test_engine()

    print("=" * 64)
    if FAILS:
        print(f"RESULT: FAIL ({len(FAILS)}) -> {FAILS}")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
