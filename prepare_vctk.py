from __future__ import annotations

from collections import defaultdict
import io
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / ".venv" / "Lib" / "site-packages"))
import numpy as np
import pyarrow.parquet as pq
import soundfile as sf
from prepare_training import BASE, RATE, write_json


def main():
    source = json.loads((BASE / "vctk" / "source.json").read_text(encoding="utf-8"))
    groups = defaultdict(list)
    origins = defaultdict(list)
    for filename in source["files"]:
        path = Path(filename)
        split = "validation" if path.name.startswith("val-") else "train"
        table = pq.ParquetFile(path)
        seen = 0
        limit = 180 if split == "validation" else 350
        for batch in table.iter_batches(batch_size=32, columns=["audio", "name"]):
            for row in batch.to_pylist():
                name = row["name"]
                match = re.search(r"p\d+", name)
                if not match:
                    raise ValueError(f"无法识别 VCTK 说话人：{name}")
                speaker = match.group(0)
                binary = row["audio"]
                if isinstance(binary, dict):
                    binary = binary["bytes"]
                audio, rate = sf.read(io.BytesIO(binary), dtype="float32", always_2d=True)
                if rate != RATE:
                    raise ValueError(f"VCTK 片段不是原生48kHz：{name}, {rate}")
                x = audio[:, 0]
                if len(x) < RATE or not np.isfinite(x).all():
                    continue
                groups[(split, speaker)].append(x)
                origins[(split, speaker)].append(name)
                seen += 1
                if seen >= limit:
                    break
            if seen >= limit:
                break
        print("VCTK_DECODED", path.name, seen, flush=True)
    training_speakers = {speaker for split, speaker in groups if split == "train"}
    validation_speakers = {speaker for split, speaker in groups if split == "validation"}
    if training_speakers & validation_speakers:
        raise ValueError("VCTK训练和验证说话人发生重叠。")
    dataset = json.loads((BASE / "dataset.json").read_text(encoding="utf-8"))
    rows = [row for row in dataset["files"] if not row.get("group", "").startswith("vctk_")]
    for (split, speaker), parts in groups.items():
        # Keep each utterance unchanged and use short zero gaps between them.
        gap = np.zeros(int(RATE * .15), dtype=np.float32)
        audio = np.concatenate([piece for part in parts for piece in (part, gap)])
        destination = BASE / "prepared" / f"vctk_{split}_{speaker}.npy"
        activity = destination.with_suffix(".activity.npy")
        np.save(destination, audio, allow_pickle=False)
        frames = audio[:len(audio) // 960 * 960].reshape(-1, 960)
        energy = 10 * np.log10(np.mean(frames ** 2, axis=1) + 1e-12)
        active = np.flatnonzero(energy > -48).astype(np.int64) * 960
        np.save(activity, active, allow_pickle=False)
        rows.append({"path": str(destination), "activity_path": str(activity), "role": "interference",
            "split": split, "group": f"vctk_{speaker}", "speaker_id": speaker,
            "source_sample_rate": RATE, "sample_rate": RATE, "resampled": False,
            "seconds": len(audio) / RATE, "frames": len(audio), "codec": "opus",
            "license": "CC-BY-4.0", "source": source["repository"], "source_revision": source["revision"],
            "source_utterances": origins[(split, speaker)], "processing": ["decode", "concatenate_with_zero_gaps"]})
    dataset["files"] = rows
    dataset["limitations"] = [
        "User recordings have not been verified for multiple speakers or music.",
        "Target recordings are lossy AAC; VCTK 48kHz mirror uses lossy Opus.",
        "Additional 16kHz multilingual interference is resampled for augmentation only.",
        "No singing training data; validation does not establish current live microphone quality."]
    dataset["fullband_interference_speakers"] = {"train": sorted(training_speakers),
                                                  "validation": sorted(validation_speakers)}
    # Preserve the manifest used before adding the fullband material.
    previous = BASE / "dataset.before_fullband.json"
    if not previous.exists():
        previous.write_text((BASE / "dataset.json").read_text(encoding="utf-8"), encoding="utf-8")
    write_json(BASE / "dataset.json", dataset)
    print("FULLBAND_DATASET_READY", dataset["fullband_interference_speakers"], flush=True)
    for split in ["train", "validation"]:
        print(split, "fullband_interference_minutes", round(sum(
            row["seconds"] for row in rows if row["role"] == "interference" and
            row["split"] == split and row["source_sample_rate"] == RATE) / 60, 2), flush=True)


if __name__ == "__main__":
    main()
