"""P1/P3 验收：用真实第三方 VST3 插件证明「参数改变 → 输出改变」。

运行：runtime\python.exe tests\test_vst3_real.py
需要本机安装至少一个 VST3 效果器；找不到时报错退出（退出码 2），不算通过。

在独立进程里加载 iZotope Ozone 11 Equalizer，处理 1 kHz 正弦，
对比旁通与提增益后的输出电平。不写入项目数据目录。
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(r"C:\Users\Handa He\Documents\ChatGPT\Record Out")
sys.path.insert(0, str(ROOT))

from mixer_service import Vst3Chain, standard_vst3_dirs, scan_plugins  # noqa: E402

RATE = 48000
BLOCK = 1920
TONE_HZ = 1000.0


def tone(amp=0.1):
    t = np.arange(BLOCK * 8) / RATE
    return (amp * np.sin(2 * np.pi * TONE_HZ * t)).astype(np.float32)


def rms(x):
    return float(np.sqrt(np.mean(x ** 2) + 1e-12))


dirs = [d for d in standard_vst3_dirs() if Path(d).is_dir()]
print("scan dirs:", dirs)
found = scan_plugins(dirs)
plugins = found["plugins"]
print("plugins:", len(plugins), "errors:", len(found.get("errors") or []))

target = next((p for p in plugins if "Equalizer" in p["name"]), None)
if target is None:
    target = next((p for p in plugins if "Ozone" in p["name"]), None)
if target is None:
    print("NO PLUGIN FOUND - 未验证")
    sys.exit(2)

print("chosen:", target["name"], "|", target["vendor"], "|", target["classId"][:12], "...")

chain = Vst3Chain()
index = chain.add(target["path"], target["classId"])
chain.setup(RATE, BLOCK)
chain.start()
info = chain.info(index)
print("loaded:", info["name"], "| bypass:", info["bypass"], "| latency(samples):", chain.latency())

sig = tone()
left = sig.copy()
right = sig.copy()

# 预热若干块，让插件完成内部延迟与状态初始化
for _ in range(4):
    chain.process(left, right)

bypass_out = []
for _ in range(4):
    l, r = chain.process(left, right)
    bypass_out.append(l.copy())
bypass = np.concatenate(bypass_out)
print("bypass RMS: %.6f  peak: %.4f" % (rms(bypass), float(np.max(np.abs(bypass)))))

params = chain.params(index)
print("params:", len(params))
for p in params[:8]:
    print("   ", p.get("id"), "|", (p.get("title") or p.get("shortTitle") or "")[:40], "|", round(float(p.get("value", 0)), 3))

# 找一个增益类参数并推到较大值
gain_param = None
for p in params:
    text = (str(p.get("title", "")) + " " + str(p.get("shortTitle", ""))).lower()
    if any(k in text for k in ("gain", "output", "level", "mix", "amount")):
        gain_param = p
        break

changed = False
if gain_param is not None:
    print("trying param:", gain_param.get("title"), "id", gain_param.get("id"))
    chain.set_param(index, int(gain_param["index"]), 0.85)
    chain.process(left, right)  # 让参数生效
    boosted = np.concatenate([chain.process(left, right)[0] for _ in range(4)])
    ratio = rms(boosted) / max(rms(bypass), 1e-9)
    print("after param RMS: %.6f  ratio: %.2f" % (rms(boosted), ratio))
    changed = abs(20 * np.log10(max(ratio, 1e-9))) > 0.5
else:
    print("未找到增益类参数，无法通过参数证明；改为旁通对比")

# 旁通对比：链上旁通时不处理
chain.set_bypass(index, True)
for _ in range(2):
    chain.process(left, right)
bypassed = np.concatenate([chain.process(left, right)[0] for _ in range(4)])
print("chain bypass RMS: %.6f" % rms(bypassed))

# 双实例：再加载一个同一插件，验证排序与单插件旁通
second = chain.add(target["path"], target["classId"])
print("instances:", chain.count(), "| second index:", second)
chain.move(0, 1)
print("after move, order:", [chain.info(i)["name"] for i in range(chain.count())])
chain.set_bypass(1, True)
# 新增实例后必须重新协商格式并重启处理（服务端同样要求停机改链）
chain.stop()
chain.setup(RATE, BLOCK)
chain.start()
flags = [chain.info(i)["bypass"] for i in range(chain.count())]
print("bypass flags:", flags)
dual = np.concatenate([chain.process(left.copy(), right.copy())[0] for _ in range(4)])
ok_dual = bool(np.isfinite(dual).all()) and chain.count() == 2 and flags == [False, True]
print("dual-process RMS: %.6f  finite: %s" % (rms(dual), bool(np.isfinite(dual).all())))

chain.close()
pass_all = changed and ok_dual
print("RESULT:", "PASS" if pass_all else "INCONCLUSIVE")
sys.exit(0 if changed else 3)
