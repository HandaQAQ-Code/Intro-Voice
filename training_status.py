from __future__ import annotations

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="查看或安全停止当前训练")
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    base = Path(__file__).resolve().parent / "data" / "training"
    if not (base / "latest_run.json").exists():
        print("尚未启动训练。")
        return
    latest = json.loads((base / "latest_run.json").read_text(encoding="utf-8"))
    run = Path(latest["directory"])
    state = json.loads((run / "status.json").read_text(encoding="utf-8"))
    print(json.dumps(state, ensure_ascii=False, indent=2))
    if args.stop and state["state"] in {"initializing", "training"}:
        (run / "STOP").touch()
        print("已请求停止，将在当前批次结束后保存检查点。")


if __name__ == "__main__":
    main()
