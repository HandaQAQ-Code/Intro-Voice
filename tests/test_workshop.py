"""创意工坊回归：编译、试听、异常隔离、市场安装。

运行：runtime\\python.exe tests\\test_workshop.py
"""
from __future__ import annotations

import http.server
import json
import shutil
import socketserver
import sys
import tempfile
import threading
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import market_service  # noqa: E402
import workshop_service as ws  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  PASS  {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAILED += 1
        print(f"  FAIL  {name}" + (f"  [{detail}]" if detail else ""))


GOOD = '''
PARAMS = [
    {"id": "amount", "name": "空气感", "min": 0.0, "max": 1.0, "default": 0.3, "step": 0.01},
    {"id": "trim", "name": "输出", "min": -12.0, "max": 6.0, "default": 0.0, "step": 0.5, "unit": "dB"},
]

def process(x, sr, params):
    high = np.diff(x, axis=0, prepend=x[:1])
    y = x + high * (params["amount"] * 2.0)
    return (y * db_to_gain(params["trim"])).astype(np.float32)
'''

BAD_SHAPE = '''
def process(x, sr, params):
    return x[:10]
'''

BAD_NAN = '''
def process(x, sr, params):
    return x * np.nan
'''

FORBIDDEN = '''
import os
def process(x, sr, params):
    return x
'''


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="ws-test-"))
    try:
        print("== 编译与保存 ==")
        saved = ws.save_plugin(temp, {"name": "空气感", "slug": "air-detail",
                                      "author": "QA", "version": "1.0.0",
                                      "license": "MIT", "description": "测试",
                                      "code": GOOD})
        check("保存并解析参数", len(saved["params"]) == 2, str([p["id"] for p in saved["params"]]))
        check("插件落盘", (temp / "data/mixer/workshop/air-detail/plugin.json").is_file())

        broken = ws.compile_manifest({"name": "x", "code": "PARAMS=[]"})
        check("缺少 process 报错", broken.fn is None and "process" in broken.error, broken.error[:40])

        forbidden = ws.compile_manifest({"name": "x", "code": FORBIDDEN})
        check("拒绝 import", forbidden.fn is None and "import" in forbidden.error, forbidden.error[:40])

        print("== 离线试听 ==")
        result = ws.test_manifest({"name": "空气感", "slug": "air-detail", "code": GOOD})
        check("试听通过", result["ok"], str(result.get("error", ""))[:60])
        check("确实改变了音频", abs(result["changeDb"]) > 0.05, f"{result['changeDb']:+.2f} dB")
        check("耗时在预算内", result["msPerBlock"] < 40.0, f"{result['msPerBlock']} ms/块")

        print("== 运行时隔离 ==")
        stages, errors = ws.build_stages(temp, {
            "order": ["air-detail", "missing"],
            "enabled": {"air-detail": True},
            "params": {"air-detail": {"amount": 0.5}},
        })
        check("缺插件只记错误", len(stages) == 1 and len(errors) == 1, str(errors)[:50])

        runner = ws.WorkshopRunner(48000)
        runner.set_stages(stages)
        block = np.random.default_rng(2).standard_normal((1024, 2)).astype(np.float32) * 0.1
        out = runner.process(block)
        check("正常输出同形状", out.shape == block.shape and np.isfinite(out).all())
        check("参数已应用", abs(stages[0].params["amount"] - 0.5) < 1e-9)

        runner.update_params("air-detail", {"amount": 0.9})
        runner.process(block)
        check("热更新参数", abs(stages[0].params["amount"] - 0.9) < 1e-9)

        # 异常插件：形状不符与 NaN 都应被旁通，输入原样返回
        for label, code in (("形状不符", BAD_SHAPE), ("NaN", BAD_NAN)):
            bad = ws.compile_manifest({"name": label, "slug": "bad", "code": code})
            stage = ws.Stage("bad", label, bad.fn, {})
            isolated = ws.WorkshopRunner(48000)
            isolated.set_stages([stage])
            got = isolated.process(block)
            check(f"{label} 被旁通", np.array_equal(got, block) and isolated.errors == 1,
                  f"errors={isolated.errors}")

        print("== 引擎集成 ==")
        import queue
        import mixer_service as ms
        from mixer_dsp import default_params, merge_params

        class StubChain:
            def count(self):
                return 0

            def process(self, left, right):
                return left, right

        class StubResampler:
            def process(self, audio):
                mono = audio[:, 0] if audio.ndim == 2 else audio
                return np.repeat(mono[:, None], 2, axis=1).astype(np.float32)

        engine = ms.MixerEngine(StubChain(), queue.Queue())
        engine.ai_mode = "off"
        engine.options.gain_db = 0.0
        engine.dsp_params = merge_params(default_params(), {"enabled": False})
        engine.dsp.set_params(engine.dsp_params)
        engine.workshop.set_stages([
            ws.Stage("x2", "双倍", lambda x, sr, p: (x * 2.0).astype(np.float32), {}),
        ])
        tone = (0.05 * np.sin(2 * np.pi * 440 * np.arange(4800) / 48000)).astype(np.float32)
        out = engine._process_block(tone[:, None], StubResampler(), ms.PeakLimiter(48000), None, None)
        ratio = float(np.sqrt(np.mean(out ** 2)) / np.sqrt(np.mean(tone ** 2)))
        check("工坊插件进入引擎链路", abs(ratio - 2.0) < 0.05, f"x{ratio:.3f}")

        print("== 市场安装 ==")
        market = temp / "plugin-market"
        (market / "plugins" / "air-detail").mkdir(parents=True)
        manifest = json.loads((temp / "data/mixer/workshop/air-detail/plugin.json").read_text(encoding="utf-8"))
        (market / "plugins" / "air-detail" / "plugin.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

        handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(  # noqa: E731
            *a, directory=str(market), **kw)
        server = socketserver.TCPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            entry = {"slug": "air-detail", "name": "空气感", "type": "workshop",
                     "manifest": f"http://127.0.0.1:{server.server_address[1]}/plugins/air-detail/plugin.json"}
            got = market_service.install(temp, entry)
            check("市场安装工坊插件", Path(got["path"]).is_dir())
            listed = market_service.installed_plugins(temp)
            check("安装清单记录", any(p["slug"] == "air-detail" for p in listed))
            market_service.uninstall(temp, "air-detail")
            check("卸载删除目录", not Path(got["path"]).exists())
        finally:
            server.shutdown()

        print("== 预设摘要 ==")
        from mixer_service import Service  # noqa: E402 - 只用到静态方法
        summary = Service._preset_summary({
            "plugins": [{}, {}],
            "config": {"aiMode": "cpu", "dsp": {"enabled": True,
                                                "hpf": {"enabled": True},
                                                "eq": {"enabled": True}},
                       "workshop": {"order": ["air-detail"]}},
        })
        check("插件数与 AI 档", summary["plugins"] == 2 and summary["aiLabel"] == "AI CPU")
        check("效果与工坊计数", summary["effects"] == ["滤波", "EQ"] and summary["workshop"] == 1,
              f"{summary['effects']} / {summary['workshop']}")
        return 0 if FAILED == 0 else 1
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    print(f"workshop tests @ {ROOT}")
    code = main()
    print(f"\n结果：{PASSED} 通过 / {FAILED} 失败")
    sys.exit(code)
