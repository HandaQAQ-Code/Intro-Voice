"""虚拟麦克风检测模块的离线回归：不触碰系统设备、不安装驱动。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import virtual_device as vd  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def fake_device(name: str, *, ins: int = 0, outs: int = 0, rate: int = 48000):
    return {"name": name, "hostapi": 0, "max_input_channels": ins,
            "max_output_channels": outs, "default_samplerate": rate}


HOSTS = [{"name": "Windows WASAPI"}]


def main():
    # 名称判定
    for name in ("CABLE Input (VB-Audio Virtual Cable)",
                 "CABLE Output (VB-Audio Virtual Cable)",
                 "VB-Audio Virtual Cable", "CABLE In 16 Bit"):
        check(f"识别端点 {name}", vd.is_cable_endpoint(name))
    for name in ("麦克风 (Realtek)", "立体声混音", "CABLE Input 16 ch"):
        check(f"排除端点 {name}", not vd.is_cable_endpoint(name))

    # 合成设备：两个端点都在 -> 已安装
    devices = [
        fake_device("麦克风 (Realtek)", ins=2),
        fake_device("CABLE Input (VB-Audio Virtual Cable)", outs=2),
        fake_device("CABLE Output (VB-Audio Virtual Cable)", ins=2),
        fake_device("扬声器 (Realtek)", outs=2),
    ]
    state = vd.detect(devices=devices, hosts=HOSTS)
    check("两端点齐全 => 已安装", state["installed"] is True, state["hint"])
    check("render = CABLE Input", "cable input" in (state["render"] or {}).get("name", "").lower())
    check("capture = CABLE Output", "cable output" in (state["capture"] or {}).get("name", "").lower())
    check("端点带 WASAPI 标签", state["render"]["host"] == "Windows WASAPI")
    check("候选数量", len(state["candidates"]) == 2, f"{len(state['candidates'])}")
    check("版权提示", "VB-Audio" in state["driverNote"])
    check("官网地址", state["downloadUrl"].startswith("https://"))

    # 只有播放端点 / 完全没有
    only_render = vd.detect(devices=[devices[1]], hosts=HOSTS)
    check("缺少录音端点 => 未安装", only_render["installed"] is False)
    none_state = vd.detect(devices=[devices[0], devices[3]], hosts=HOSTS)
    check("无虚拟端点 => 未安装", none_state["installed"] is False)
    check("未安装时给出引导文案", "安装" in none_state["hint"])

    # 本机包查找只返回路径，不做复制/安装
    pack = vd.find_local_pack()
    check("包查找结构", set(("present", "dir", "setup", "readme")) <= set(pack))
    if pack["present"]:
        check("包路径存在", Path(pack["setup"]).is_file(), pack["setup"])
    check("hint 随包状态变化",
          ("官方驱动包" in vd.hint(False, {"present": True}))
          and ("官网下载" in vd.hint(False, {"present": False})))

    # 真实枚举（本机可能没有驱动），结构必须稳定
    live = vd.status()
    check("status 结构完整", set(("installed", "render", "capture", "hint", "pack", "driverNote")) <= set(live))
    check("status 不报错", live["error"] == "", live["error"])

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
