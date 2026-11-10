"""新手引导后端：状态字段、音质档落盘、输入源切换的回归测试。

运行：runtime\\python.exe tests\\test_guide.py

覆盖两个曾经真实出现过的 bug：
* _devices() 的 cableOutput 会兜底成任意 WASAPI 输出，导致「虚拟声卡已安装」恒为真；
* set_options 静默丢掉 aiMode，导致引导里选的档位根本没写进配置。
"""
from __future__ import annotations

import json
import logging
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mixer_service as m  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}  {detail}")


def call(service, cmd: str, **params):
    reply = service.handle({"cmd": cmd, "id": 1, **params})
    if not reply.get("ok"):
        raise AssertionError(f"{cmd} -> {reply.get('error')}")
    return reply["result"]


def main() -> int:
    # 负例是故意触发的错误命令，服务端会打 traceback；测试输出只留断言结果。
    logging.disable(logging.CRITICAL)
    # 测试不能碰用户真实的 settings.json。
    with tempfile.TemporaryDirectory() as tmp:
        original = m.SETTINGS_FILE
        m.SETTINGS_FILE = Path(tmp) / "settings.json"
        try:
            service = m.Service()

            status = call(service, "guide_status")
            for key in ("virtual", "voice", "mobile", "inputSource",
                        "mobileConnected", "virtualInstalled", "cableInLabel"):
                check(f"guide_status 含 {key}", key in status)

            virtual = status["virtual"]
            for key in ("installed", "packPresent", "vendorUrl",
                        "downloadUrl", "hint", "driverNote"):
                check(f"virtual 含 {key}", key in virtual)

            voice = status["voice"]
            for key in ("exists", "seconds", "sizeMb", "path"):
                check(f"voice 含 {key}", key in voice)

            mobile = status["mobile"]
            for key in ("running", "ip", "port", "pin", "installUrl",
                        "clients", "apkReady", "levelDb"):
                check(f"mobile 含 {key}", key in mobile)

            # 真实 CABLE 端点存在时，cableInLabel 不能是兜底的任意输出。
            devices = service._devices()
            if devices.get("cableDetected"):
                check("cableInLabel 指向 CABLE", "cable" in status["cableInLabel"].lower(),
                      status["cableInLabel"])
                check("virtualInstalled 为真", status["virtualInstalled"] is True)
            check("virtual.installed 与 virtualInstalled 一致",
                  virtual["installed"] == status["virtualInstalled"])

            # 声纹 / 手机状态命令都要能独立返回。
            check("guide_voice_status 空闲态",
                  call(service, "guide_voice_status")["recording"] is False)
            check("guide_mobile_status 返回 running",
                  "running" in call(service, "guide_mobile_status"))

            # set_options 必须真的把 aiMode 写进 settings（旧版会静默丢弃）。
            call(service, "set_options", aiMode="cpu")
            check("set_options 写入 aiMode",
                  service.settings.get("aiMode") == "cpu",
                  str(service.settings.get("aiMode")))
            saved = json.loads(m.SETTINGS_FILE.read_text(encoding="utf-8"))
            check("aiMode 已落盘", saved.get("aiMode") == "cpu", str(saved.get("aiMode")))

            # 引导专用选档命令：合法档位生效，非法档位报错。
            q = call(service, "guide_quality", aiMode="off")
            check("guide_quality off 生效", q["aiMode"] == "off")
            err = service.handle({"cmd": "guide_quality", "id": 2, "aiMode": "bogus"})
            check("guide_quality 拒绝非法档", err.get("ok") is False)

            # 输入源切换：手机 / 本机都要落盘。
            mob = call(service, "guide_source", inputSource="mobile")
            check("guide_source mobile 生效", mob["inputSource"] == "mobile")
            back = call(service, "guide_source", inputSource="device")
            check("guide_source device 生效", back["inputSource"] == "device")
            invalid = service.handle(
                {"cmd": "guide_source", "id": 3, "inputSource": "bluetooth"})
            check("guide_source 拒绝非法来源", invalid.get("ok") is False)
        finally:
            m.SETTINGS_FILE = original

    # 静态检查：引导需要的命令都在服务端注册过。
    source = (ROOT / "mixer_service.py").read_text(encoding="utf-8")
    for cmd in ("cmd_guide_voice_start", "cmd_guide_voice_cancel",
                "cmd_guide_cable_open", "cmd_guide_mobile_start",
                "cmd_guide_mobile_status", "cmd_guide_source",
                "cmd_guide_quality"):
        check(f"存在 {cmd}", f"def {cmd}(" in source)

    print(f"\n结果：{PASSED} 通过 / {FAILED} 失败")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
