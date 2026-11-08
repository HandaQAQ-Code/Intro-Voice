from __future__ import annotations

from pathlib import Path
import os
import shutil
import types

import numpy as np
import torch
import torchaudio
import yaml

from super_resolution import StreamingSuperResolution

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "data" / "super_resolution" / "lavasr"
REVISION = "b98dc8be472da45ab7b6346ad7997e1dfeb5911d"
LATENCY_PROFILES = {
    "balanced": {"label": "平衡（224 ms 前瞻）", "lookahead": 3584, "hop": 512, "block_ms": 32, "prefill_ms": 64},
    "fast": {"label": "低延迟（192 ms 前瞻）", "lookahead": 3072, "hop": 512, "block_ms": 32, "prefill_ms": 64},
    "quality": {"label": "完整上下文（352 ms 前瞻）", "lookahead": 5632, "hop": 1024, "block_ms": 40, "prefill_ms": 80},
}


def load_lavasr():
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA 不可用。请通过 start_gpu.bat 启动，并检查 NVIDIA 驱动和 CUDA 版 PyTorch。")
    from vocos import Vocos
    from vocos.feature_extractors import MelSpectrogramFeatures
    from vocos.heads import ISTFTHead
    from vocos.models import VocosBackbone

    ASSETS.mkdir(parents=True, exist_ok=True)
    for name in ("config.yaml", "pytorch_model.bin"):
        if not (ASSETS / name).is_file():
            from huggingface_hub import hf_hub_download
            source = hf_hub_download("YatharthS/LavaSR", f"enhancer_v2/{name}", revision=REVISION)
            pending = ASSETS / (name + ".pending")
            shutil.copyfile(source, pending)
            os.replace(pending, ASSETS / name)
    config = yaml.safe_load((ASSETS / "config.yaml").read_text(encoding="utf-8"))
    args = config["feature_extractor"]["init_args"]
    feature = MelSpectrogramFeatures(**{key: args[key] for key in
        ("sample_rate", "n_fft", "hop_length", "n_mels", "padding")})
    # Match the published filterbank, including its configured 44100 Hz value.
    # The official inference path feeds a 48 kHz waveform to this checkpoint.
    feature.mel_spec = torchaudio.transforms.MelSpectrogram(
        **{key: args[key] for key in ("sample_rate", "n_fft", "hop_length", "n_mels",
                                      "f_min", "f_max", "norm", "mel_scale")},
        center=False, power=1)
    model = Vocos(feature, VocosBackbone(**config["backbone"]["init_args"]),
                  ISTFTHead(**config["head"]["init_args"]))
    model.load_state_dict(torch.load(ASSETS / "pytorch_model.bin", map_location="cpu",
                                     weights_only=True), strict=True)

    def head_forward(self, x):
        magnitude, phase = self.out(x).transpose(1, 2).chunk(2, dim=1)
        magnitude = magnitude.exp().clamp(max=1e3)
        return self.istft(magnitude * (phase.cos() + 1j * phase.sin()))

    model.head.forward = types.MethodType(head_forward, model.head)
    # The ISTFT window envelope depends only on shape, not on the microphone.
    # Cache it and check it once instead of recomputing and synchronizing the
    # GPU with a Python assertion on every audio block. This also allows graphs.
    envelopes = {}

    def istft_forward(self, spectrum):
        frames = spectrum.shape[-1]
        pad = (self.win_length - self.hop_length) // 2
        size = (frames - 1) * self.hop_length + self.win_length
        wave = torch.fft.irfft(spectrum, self.n_fft, dim=1) * self.window[None, :, None]
        wave = torch.nn.functional.fold(wave, output_size=(1, size), kernel_size=(1, self.win_length),
                                        stride=(1, self.hop_length))[:, 0, 0, pad:-pad]
        if frames not in envelopes:
            squared = self.window.square().expand(1, frames, -1).transpose(1, 2)
            envelope = torch.nn.functional.fold(squared, output_size=(1, size),
                kernel_size=(1, self.win_length), stride=(1, self.hop_length)).reshape(-1)[pad:-pad]
            if not bool((envelope > 1e-11).all()):
                raise RuntimeError("超分合成窗口无效。")
            envelopes[frames] = envelope
        return wave / envelopes[frames]

    model.head.istft.forward = types.MethodType(istft_forward, model.head.istft)
    device = torch.device("cuda:0")
    # FP32 throughout: changing the synthesis precision can change its timbre.
    torch.set_num_threads(2)
    model = model.eval().to(device)
    return model, device


class LavaBackend:
    def __init__(self, model, device):
        self.model, self.device = model, device
        self.runners = {}

    @torch.inference_mode()
    def prepare(self, length):
        if length in self.runners:
            return
        static = torch.zeros((1, length), device=self.device)
        stream = torch.cuda.Stream()
        stream.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(stream):
            for _ in range(3):
                self.model(static)
        torch.cuda.current_stream().wait_stream(stream)
        torch.cuda.synchronize()
        graph = torch.cuda.CUDAGraph()
        try:
            with torch.cuda.graph(graph):
                output = self.model(static)
            graph.replay()
            torch.cuda.synchronize()
        except RuntimeError:
            graph = None
            output = self.model(static)
        self.runners[length] = (static, output, graph)

    @torch.inference_mode()
    def infer(self, waveform, start=0, end=None):
        x = torch.from_numpy(waveform).unsqueeze(0)
        if len(waveform) > 96000:
            output = self.model(x.to(self.device))
        else:
            self.prepare(len(waveform))
            static, cached, graph = self.runners[len(waveform)]
            static.copy_(x)
            if graph is None:
                output = self.model(static)
            else:
                graph.replay()
                output = cached
        # Copy only this hop back to the CPU, rather than all context samples.
        return output[0, start:end].float().cpu().numpy().copy()


class StreamingLavaSR(StreamingSuperResolution):
    # 27 convolution frames in each direction, plus STFT/ISTFT support.
    # 33 frames of context preserve the complete receptive field. All crops
    # and hops align to the model's 512-sample STFT grid at the 48 kHz output.
    HOP = 1024                 # 64 ms at 16 kHz
    CONTEXT = 5632             # 352 ms on either side
    RESAMPLE_QUALITY = "HQ"

    def __init__(self, backend, profile="balanced"):
        params = LATENCY_PROFILES[profile]
        super().__init__(backend, hop=params["hop"], lookahead=params["lookahead"])
        self.backend = backend if isinstance(backend, LavaBackend) else LavaBackend(*backend)
        self.model, self.device = self.backend.model, self.backend.device

    @torch.inference_mode()
    def _generate(self, context: np.ndarray):
        import soxr
        waveform = soxr.resample(context, 16000, 48000, quality="HQ").astype(np.float32)
        return self.backend.infer(waveform)

    def _bands(self, context, start, end):
        import soxr
        waveform = soxr.resample(context, 16000, 48000, quality="HQ").astype(np.float32)
        return self.backend.infer(waveform, start, end), waveform[start:end]


def prepare_gpu_model(profile="balanced"):
    backend = LavaBackend(*load_lavasr())
    stream = StreamingLavaSR(backend, profile)
    backend.prepare((stream.CONTEXT + stream.HOP + stream.lookahead) * 3)
    return backend
