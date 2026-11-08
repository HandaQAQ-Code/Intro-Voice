from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / ".venv" / "Lib" / "site-packages"))
import numpy as np

BASE = ROOT / "data" / "training"
RATE = 48000
GIT_REVISION = "9bd9844a227bb6aa57e55588d8d0e961fcff1c46"
WEIGHT_REVISION = "dd6818d00f50c836fed43a6243ebe49116de5964"
SOURCES = ["U3P2", "U3P2n", "U1P1", "U1P2", "U2P1n", "U2P2", "U2P2n", "U3P1", "U3P1n", "U3P11"]
# Hold out complete January 17/18 recordings. January 29/31 train recordings
# never contribute adjacent segments to this validation set.
VALIDATION = {"U3P2", "U2P2", "U3P1", "U3P11"}


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(pending, path)


def download(url: str, destination: Path):
    if destination.exists() and destination.stat().st_size > 0:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.with_suffix(destination.suffix + ".part")
    last = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "personal-voice-training"})
            with urllib.request.urlopen(request, timeout=45) as response, pending.open("wb") as target:
                while block := response.read(1024 * 1024):
                    target.write(block)
            os.replace(pending, destination)
            print("Downloaded", destination.name, destination.stat().st_size, flush=True)
            return destination
        except Exception as exc:
            last = exc
            print("Download retry", attempt + 1, destination.name, str(exc), flush=True)
    raise RuntimeError(f"下载失败：{url}") from last


def assets():
    files = ["model/dpdfnet_48khz_hr.py", "model/modules.py", "model/multiframe.py", "model/utils.py",
             "model/init_norms.py", "onnx_model/dpdfnet_48khz_hr.py", "onnx_model/export_utils.py",
             "onnx_model/export_dpdfnet_48khz_hr_to_onnx.py", "onnx_model/layers.py",
             "onnx_model/multiframe.py", "onnx_model/utils.py", "onnx_model/init_norms.py", "LICENSE"]
    def fetch(name):
        return download(f"https://raw.githubusercontent.com/ceva-ip/DPDFNet/{GIT_REVISION}/{name}",
                        BASE / "vendor" / "dpdfnet" / name)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(fetch, files))
    download(f"https://huggingface.co/Ceva-IP/DPDFNet/resolve/{WEIGHT_REVISION}/checkpoints/dpdfnet2_48khz_hr.pth",
             BASE / "pretrained" / "dpdfnet2_48khz_hr.pth")
    write_json(BASE / "vendor" / "dpdfnet" / "source.json", {
        "repository": "https://github.com/ceva-ip/DPDFNet", "revision": GIT_REVISION,
        "weights_repo": "Ceva-IP/DPDFNet", "weights_revision": WEIGHT_REVISION,
        "license": "Apache-2.0"})


def public_interference():
    repository = "Ceva-IP/DPDFNet_EvalSet"
    with urllib.request.urlopen(f"https://huggingface.co/api/datasets/{repository}", timeout=30) as response:
        meta = json.load(response)
    revision = meta["sha"]
    languages = ["arabic", "dutch", "french", "german", "hungarian", "korean", "polish",
                 "portuguese", "russian", "spanish", "turkish", "vietnamese"]
    def fetch(language):
        name = f"{language}_airport_snr0_rt60none_0_mixture_clean.wav"
        url = f"https://huggingface.co/datasets/{repository}/resolve/{revision}/Clean/{name}"
        path = download(url, BASE / "interference" / name)
        return {"path": str(path), "split": "validation" if language in {"german", "korean", "vietnamese"} else "train",
                "group": language, "source": url, "license": "Apache-2.0"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(fetch, languages))
    write_json(BASE / "interference_sources.json", {"repository": repository, "revision": revision,
        "purpose": "local_personal_model_interference_training_not_official_benchmark", "files": rows,
        "note": "该数据原始为16kHz，仅干扰样本会上采样；个人录音与模型保持原生48kHz。高频他人声泛化仍需实录验证。"})


def probe(path: Path):
    value = subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                                     "-of", "json", str(path)], encoding="utf-8")
    meta = json.loads(value)
    stream = next(item for item in meta["streams"] if item["codec_type"] == "audio")
    return stream, meta["format"]


def prepare_audio(path: Path, role: str, split: str, group: str):
    stream, container = probe(path)
    native_rate, channels = int(stream["sample_rate"]), int(stream["channels"])
    if role == "target" and native_rate != RATE:
        raise ValueError(f"个人录音必须原生48kHz：{path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    name = f"{role}_{path.stem}_{digest[:12]}"
    destination = BASE / "prepared" / f"{name}.npy"
    activity = destination.with_suffix(".activity.npy")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Decode full-band first, then retain a single channel without filtering.
    if not destination.exists():
        raw = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0",
                                       "-ar", str(RATE), "-f", "f32le", "-acodec", "pcm_f32le", "-"])
        audio = np.frombuffer(raw, dtype="<f4").reshape(-1, channels)
        power = np.mean(audio.astype(np.float64) ** 2, axis=0)
        channel = int(np.argmax(power))
        x = audio[:, channel].copy()
        if not np.isfinite(x).all():
            raise ValueError(f"无效音频：{path}")
        pending = destination.with_suffix(".pending.npy")
        np.save(pending, x, allow_pickle=False)
        os.replace(pending, destination)
    else:
        x = np.load(destination, mmap_mode="r")
        channel = None
    frames = x[:len(x) // 960 * 960].reshape(-1, 960)
    db = 10 * np.log10(np.mean(frames ** 2, axis=1) + 1e-12)
    # Activity is only a sampling aid, not speech/speaker verification.
    threshold = max(-48.0, min(float(np.percentile(db, 10)) + 10, float(np.percentile(db, 90)) - 8))
    active = np.flatnonzero(db > threshold).astype(np.int64) * 960
    np.save(activity, active, allow_pickle=False)
    row = {"path": str(destination), "activity_path": str(activity), "role": role, "split": split,
           "group": group, "original": str(path), "sha256": digest, "source_sample_rate": native_rate,
           "sample_rate": RATE, "resampled": native_rate != RATE, "source_channels": channels,
           "selected_channel_zero_based": channel, "frames": len(x), "seconds": len(x) / RATE,
           "active_energy_seconds_estimate": len(active) * .02,
           "codec": stream["codec_name"], "creation_time": container.get("tags", {}).get("creation_time"),
           "human_reviewed": False, "processing": ["decode", "select_one_channel"] + (["resample_interference_only"] if native_rate != RATE else [])}
    print("Prepared", role, split, path.name, f"{len(x) / RATE:.1f}s", flush=True)
    return row


def prepare():
    jobs = [(Path.home() / "Downloads" / f"{name}.m4a", "target",
             "validation" if name in VALIDATION else "train", name) for name in SOURCES]
    public = json.loads((BASE / "interference_sources.json").read_text(encoding="utf-8"))
    jobs += [(Path(row["path"]), "interference", row["split"], row["group"]) for row in public["files"]]
    with ThreadPoolExecutor(max_workers=3) as pool:
        rows = list(pool.map(lambda args: prepare_audio(*args), jobs))
    splits = {"train": set(), "validation": set()}
    for row in rows:
        splits[row["split"]].add(row["sha256"])
    if splits["train"] & splits["validation"]:
        raise ValueError("发现跨训练/验证集的重复原文件。")
    write_json(BASE / "dataset.json", {"schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(), "sample_rate": RATE,
        "split_policy": "whole_target_recordings_by_capture_day_and_interference_language_group",
        "target_train_dates": ["2023-01-29", "2023-01-31"],
        "target_validation_dates": ["2023-01-17", "2023-01-18"],
        "limitations": ["User recordings have not been verified for multiple speakers or music.",
                        "Interference is originally 16kHz; high-band rejection is not established.",
                        "No singing training data; validation does not guarantee current live microphone quality."],
        "files": rows})
    print("DATASET_READY", json.dumps({f"{role}_{split}_minutes": round(sum(
        row["seconds"] for row in rows if row["role"] == role and row["split"] == split) / 60, 2)
        for role in ["target", "interference"] for split in ["train", "validation"]}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", action="store_true")
    parser.add_argument("--interference", action="store_true")
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    if args.assets:
        assets()
    if args.interference:
        public_interference()
    if args.prepare:
        prepare()


if __name__ == "__main__":
    main()
