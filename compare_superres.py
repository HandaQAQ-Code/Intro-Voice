from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import time
import types

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / ".venv" / "Lib" / "site-packages"))
import numpy as np
from scipy.signal import welch
import soundfile as sf
import soxr
import torch
import torchaudio
import yaml
import voxlock
from super_resolution import enhance_array, prepare_model


def load_lavasr():
    from vocos import Vocos
    from vocos.feature_extractors import MelSpectrogramFeatures
    from vocos.heads import ISTFTHead
    from vocos.models import VocosBackbone
    root = ROOT / "data" / "super_resolution" / "lavasr"
    config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    args = config["feature_extractor"]["init_args"]
    feature = MelSpectrogramFeatures(**{key: args[key] for key in
        ["sample_rate", "n_fft", "hop_length", "n_mels", "padding"]})
    # PyPI Vocos 0.1 predates these optional filterbank constructor arguments.
    # Build exactly the transform specified by the published LavaSR v2 config.
    feature.mel_spec = torchaudio.transforms.MelSpectrogram(
        **{key: args[key] for key in ["sample_rate", "n_fft", "hop_length", "n_mels",
                                      "f_min", "f_max", "norm", "mel_scale"]},
        center=False, power=1)
    backbone = VocosBackbone(**config["backbone"]["init_args"])
    head = ISTFTHead(**config["head"]["init_args"])
    model = Vocos(feature, backbone, head)
    model.load_state_dict(torch.load(root / "pytorch_model.bin", map_location="cpu", weights_only=True), strict=True)

    def head_forward(self, x):
        x = self.out(x).transpose(1, 2)
        magnitude, phase = x.chunk(2, dim=1)
        magnitude = magnitude.exp().clamp(max=1e3)
        spectrum = magnitude * (phase.cos() + 1j * phase.sin())
        return self.istft(spectrum)

    # Match the inference head from the official LavaSR implementation.
    model.head.forward = types.MethodType(head_forward, model.head)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = model.eval().to(device)
    return model, device


@torch.inference_mode()
def lava_enhance(audio16, model, device):
    source = soxr.resample(audio16, 16000, 48000, quality="HQ").astype(np.float32)
    padded = np.pad(source, (0, 512))
    waveform = torch.from_numpy(padded).unsqueeze(0).to(device)
    prediction = model(waveform)[0, :len(source)]
    if len(prediction) != len(source):
        raise RuntimeError("LavaSR 输出长度不足。")
    source_tensor = torch.from_numpy(source).to(device)
    original = torch.fft.rfft(source_tensor)
    generated = torch.fft.rfft(prediction)
    bins = len(original)
    cutoff = int((8000 / 24000) * bins)
    transition = 1024
    start, end = cutoff - transition // 2, cutoff + transition // 2
    mask = torch.ones(bins, device=device)
    mask[:start] = 0
    x = torch.linspace(0, 1, end - start, device=device)
    mask[start:end] = 3 * x.square() - 2 * x.pow(3)
    output = torch.fft.irfft(original + (generated - original) * mask, n=len(source))
    return output.cpu().numpy().astype(np.float32)


def spectrum_summary(audio, rate):
    frequencies, power = welch(audio, rate, nperseg=2048)
    return {"rms_dbfs": round(float(10 * np.log10(np.mean(audio.astype(np.float64) ** 2) + 1e-12)), 2),
            "high_band_fraction_db": round(float(10 * np.log10(
                power[frequencies > 8000].sum() / max(power.sum(), 1e-30) + 1e-30)), 2)}


def main():
    parser = argparse.ArgumentParser(description="生成同一段录音的超分试听对照")
    parser.add_argument("--input", type=Path, default=Path.home() / "Downloads" / "U3P11.m4a")
    parser.add_argument("--start", type=float, default=30)
    parser.add_argument("--duration", type=float, default=12)
    args = parser.parse_args()
    output = ROOT / "data" / "super_resolution" / ("comparison_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    output.mkdir(parents=True, exist_ok=False)
    raw = subprocess.check_output(["ffmpeg", "-v", "error", "-ss", str(args.start),
        "-t", str(args.duration), "-i", str(args.input), "-ac", "1", "-ar", "48000", "-f", "f32le", "-"])
    reference = np.frombuffer(raw, dtype=np.float32).copy()
    low = soxr.resample(reference, 48000, 16000, quality="HQ").astype(np.float32)
    sample = voxlock.load_audio(ROOT / "data" / "target.wav")
    extractor = voxlock.VoxLock(threads=2)
    isolated = extractor.isolate(low, sample=sample)
    session = prepare_model()
    begun = time.perf_counter()
    flash = enhance_array(isolated, session, .7)
    elapsed_flash = time.perf_counter() - begun
    torch.set_num_threads(4)
    model, device = load_lavasr()
    begun = time.perf_counter()
    lava = lava_enhance(isolated, model, device)
    elapsed_lava = time.perf_counter() - begun
    ordinary = soxr.resample(isolated, 16000, 48000, quality="HQ").astype(np.float32)
    # A single shared gain keeps the A/B comparison meaningful and avoids clip
    # loudness being mistaken for restored quality. Nothing is peak-normalized
    # independently; FLOAT WAV does not hard-clip at +/-1.
    clips = [("01_original_48k.wav", reference, 48000),
             ("02_isolated_16k.wav", isolated, 16000),
             ("03_isolated_resample_only_48k.wav", ordinary, 48000),
             ("04_isolated_flashsr_48k.wav", flash, 48000),
             ("05_isolated_lavasr2_48k.wav", lava, 48000)]
    stats = {}
    for name, audio, rate in clips:
        if not np.isfinite(audio).all():
            raise RuntimeError(f"对比音频异常：{name}")
        sf.write(output / name, audio, rate, subtype="FLOAT")
        stats[name] = spectrum_summary(audio, rate)
    report = {"source": str(args.input), "start_seconds": args.start, "duration_seconds": args.duration,
              "flash_cpu_seconds": round(elapsed_flash, 3), "lava_device": str(device),
              "lava_seconds": round(elapsed_lava, 3), "files": stats,
              "note": "高频能量增加不代表恢复正确。FlashSR按流式窗口处理；LavaSR整段处理，耗时不等于流式延迟。",
              "sources": {"flashsr": "https://github.com/ysharma3501/FlashSR",
                          "lavasr": "https://github.com/ysharma3501/LavaSR",
                          "lava_weights_revision": "b98dc8be472da45ab7b6346ad7997e1dfeb5911d"}}
    (output / "comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT / "data" / "super_resolution" / "latest_comparison.json").write_text(
        json.dumps({"directory": str(output)}, ensure_ascii=False), encoding="utf-8")
    print("COMPARISON_READY", output, flush=True)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
