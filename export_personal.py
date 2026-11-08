from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / ".venv" / "Lib" / "site-packages"))
BASE = ROOT / "data" / "training"
sys.path.insert(0, str(BASE / "vendor" / "dpdfnet"))
import torch
from onnx_model.export_dpdfnet_48khz_hr_to_onnx import (
    add_meta_data, build_meta_data, build_model, export_onnx)
from onnx_model.export_utils import check_single_file_onnx, validate_onnx_runtime


def main():
    parser = argparse.ArgumentParser(description="导出个人48kHz模型供直播程序使用")
    parser.add_argument("--run", type=Path)
    args = parser.parse_args()
    run = args.run or Path(json.loads((BASE / "latest_run.json").read_text(encoding="utf-8"))["directory"])
    state = json.loads((run / "status.json").read_text(encoding="utf-8"))
    if state["state"] != "completed":
        raise RuntimeError("训练尚未完成。请查看 training_status.bat，完成后再导出。")
    checkpoint = run / "best_weights.pth"
    checksum = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    output = ROOT / "data" / "models" / "personal48k"
    output.mkdir(parents=True, exist_ok=True)
    destination = output / "model.onnx"
    metadata = output / "model.json"
    if destination.exists() and metadata.exists():
        existing = json.loads(metadata.read_text(encoding="utf-8"))
        if existing.get("checkpoint_sha256") == checksum and existing.get("export_validation_passed"):
            print("个人48kHz模型已就绪：", destination)
            return
    torch.set_num_threads(2)
    model = build_model(SimpleNamespace(checkpoint=checkpoint, dprnn_num_blocks=2))
    pending = output / "model.pending.onnx"
    wrapper = export_onnx(model, pending, opset=17, use_dynamic_axes=False, exporter="legacy")
    add_meta_data(pending, build_meta_data(model))
    check_single_file_onnx(pending)
    validate_onnx_runtime(wrapper, model, pending)
    if destination.exists():
        shutil.copy2(destination, output / "model.previous.onnx")
    os.replace(pending, destination)
    info = {"sample_rate": 48000, "model_type": "personal_target_filter",
            "target_identity": "fixed_user", "run": str(run), "checkpoint": str(checkpoint),
            "checkpoint_sha256": checksum, "export_validation_passed": True,
            "live_quality_verified": False,
            "metrics": json.loads((run / "best_metrics.json").read_text(encoding="utf-8")),
            "note": "导出数值检查通过不代表直播音质达标，先试听再用于直播。"}
    temp = output / "model.pending.json"
    temp.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    if metadata.exists():
        shutil.copy2(metadata, output / "model.previous.json")
    os.replace(temp, metadata)
    print("PERSONAL_48K_READY", destination)


if __name__ == "__main__":
    main()
