from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import random
import sys
import time
import traceback
import types

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / ".venv" / "Lib" / "site-packages"))
BASE = ROOT / "data" / "training"
sys.path.insert(0, str(BASE / "vendor" / "dpdfnet" / "model"))
import numpy as np
from scipy.signal import lfilter
import soundfile as sf
import torch
from torch import nn
from dpdfnet_48khz_hr import DPDFNet48HR

SR = 48000


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def atomic_json(path: Path, value):
    temp = path.with_suffix(path.suffix + ".pending")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    # Virus scanners may briefly hold a just-opened JSON on Windows.
    for attempt in range(10):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(.05)


def save_checkpoint(path: Path, value):
    pending = path.with_suffix(path.suffix + ".pending")
    torch.save(value, pending)
    os.replace(pending, path)


def build_model(device: torch.device):
    model = DPDFNet48HR(dprnn_num_blocks=2, stateful=False, norm_stateful=False)
    # Use exactly the same synthesis gain for training and inference. The
    # upstream training-specific normalized ISTFT has a different amplitude.
    model.apply_istft = types.MethodType(
        lambda self, spec: self.istft(spec.transpose(1, 2)) / self.wnorm, model)
    # Upstream empirical normalization initializers create CPU tensors. Register
    # movable, non-persistent buffers without changing checkpoint/export keys.
    for normalizer, method, size in [(model.erb_norm, "get_ampirical_mu_0", 481),
                                      (model.spec_norm, "get_ampirical_s_0", 96)]:
        normalizer.register_buffer("_personal_initial", getattr(normalizer.init_vals, method)(size), persistent=False)
        setattr(normalizer.init_vals, method, lambda n, normalizer=normalizer: normalizer._personal_initial[:n])
    return model.to(device)


class AudioPool:
    def __init__(self, rows: list[dict], role: str, split: str):
        self.rows = [row for row in rows if row["role"] == role and row["split"] == split]
        if not self.rows:
            raise ValueError(f"缺少 {role}/{split} 数据")
        self.audio = [np.load(row["path"], mmap_mode="r") for row in self.rows]
        self.active = [np.load(row["activity_path"]) for row in self.rows]
        self.weights = np.asarray([max(1, len(item)) for item in self.active], np.float64)
        self.weights /= self.weights.sum()

    def crop(self, rng: np.random.Generator, samples: int):
        for attempt in range(12):
            index = int(rng.choice(len(self.audio), p=self.weights))
            audio, active = self.audio[index], self.active[index]
            center = int(rng.choice(active)) if len(active) else 0
            start = int(np.clip(center - int(rng.integers(0, samples)), 0, max(0, len(audio) - samples)))
            x = np.array(audio[start:start + samples], dtype=np.float32)
            if len(x) < samples:
                x = np.pad(x, (0, samples - len(x)))
            rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2) + 1e-12))
            if rms > .002 or attempt == 11:
                return x / max(rms, .002)


class Mixtures:
    def __init__(self, rows: list[dict], split: str, seconds: float, device: torch.device):
        self.target = AudioPool(rows, "target", split)
        self.interference = AudioPool(rows, "interference", split)
        self.samples = int(seconds * SR) // 480 * 480
        self.device = device

    def batch(self, rng: np.random.Generator, batch_size: int, forced_kind: str | None = None):
        mixtures, targets, kinds = [], [], []
        for _ in range(batch_size):
            kind = forced_kind or str(rng.choice(["overlap", "clean", "absent", "silence"], p=[.50, .22, .25, .03]))
            target = self.target.crop(rng, self.samples)
            other = self.interference.crop(rng, self.samples)
            # Random gain and a mild microphone response change reduce dependence
            # on the old recorder's loudness/colour. Never synthesize new voices.
            if forced_kind is None and rng.random() < .35:
                a = float(rng.uniform(-.25, .25))
                target = lfilter([1, a], [1], target).astype(np.float32)
            level = 10 ** (float(rng.uniform(-30, -16)) / 20)
            target *= level
            other *= level * 10 ** (-float(rng.uniform(-5, 12)) / 20)
            if forced_kind is None and rng.random() < .25:
                second = self.interference.crop(rng, self.samples) * level * .4
                other = other + second
            noise = rng.standard_normal(self.samples, dtype=np.float32)
            if rng.random() < .5:
                noise = lfilter([1], [1, -.8], noise).astype(np.float32) * .6
            noise *= level * 10 ** (-float(rng.uniform(18, 35)) / 20)
            if kind == "overlap":
                mixture = target + other + noise
                # Independent pauses teach retention and rejection across turns.
                if forced_kind is None and rng.random() < .5:
                    begin = int(rng.integers(0, max(1, self.samples // 2)))
                    count = int(rng.integers(self.samples // 8, self.samples // 2))
                    window = np.ones(self.samples, dtype=np.float32)
                    window[begin:begin + count] = 0
                    ramp = min(240, count // 4)
                    window[begin:begin + ramp] = np.linspace(1, 0, ramp)
                    window[begin + count - ramp:begin + count] = np.linspace(0, 1, ramp)
                    target *= window
                    mixture = target + other + noise
            elif kind == "clean":
                mixture = target + noise
            elif kind == "absent":
                mixture = other + noise
                target.fill(0)
            else:
                mixture = noise
                target.fill(0)
            scale = min(1.0, .85 / max(float(np.max(np.abs(mixture))), 1e-9))
            mixtures.append((mixture * scale).astype(np.float32))
            targets.append((target * scale).astype(np.float32))
            kinds.append(kind)
        return (torch.from_numpy(np.stack(mixtures)).to(self.device),
                torch.from_numpy(np.stack(targets)).to(self.device), kinds)


class FidelityLoss(nn.Module):
    def __init__(self, device):
        super().__init__()
        self.windows = {n: torch.hann_window(n, device=device) for n in (512, 1024, 2048)}

    def forward(self, prediction, target, mixture):
        # Absolute-scale errors preserve the target's gain and phase. For absent
        # targets use input energy as reference, so silence is the optimum.
        target_energy = target.square().mean(-1)
        input_energy = mixture.square().mean(-1)
        present = target_energy > 1e-8
        reference = torch.where(present, target_energy, input_energy).clamp_min(1e-7)
        error = prediction - target
        mse = error.square().mean(-1) / reference
        l1 = error.abs().mean(-1) / reference.sqrt()
        loss = (mse + .4 * l1).mean()
        spectral = prediction.new_zeros(())
        for size, window in self.windows.items():
            p = torch.stft(prediction, size, size // 4, window=window, return_complex=True)
            t = torch.stft(target, size, size // 4, window=window, return_complex=True)
            mix = torch.stft(mixture, size, size // 4, window=window, return_complex=True)
            pm, tm, mm = p.abs().clamp_min(1e-7), t.abs().clamp_min(1e-7), mix.abs().clamp_min(1e-7)
            # Give frequencies above 8 kHz an explicit preservation term rather
            # than allowing low-frequency energy to dominate every loss.
            for section, weight in [(slice(None), 1.0), (slice(size // 6, None), .3)]:
                pc, tc, mc = pm[:, section].pow(.3), tm[:, section].pow(.3), mm[:, section].pow(.3)
                denominator = torch.where(present, tc.mean((1, 2)), mc.mean((1, 2))).clamp_min(.02)
                spectral = spectral + weight * ((pc - tc).abs().mean((1, 2)) / denominator).mean()
        return loss + .5 * spectral / len(self.windows)


def si_sdr(prediction, target):
    p = prediction - prediction.mean(-1, keepdim=True)
    t = target - target.mean(-1, keepdim=True)
    projected = (p * t).sum(-1, keepdim=True) * t / t.square().sum(-1, keepdim=True).clamp_min(1e-10)
    return 10 * torch.log10((projected.square().sum(-1) + 1e-10) /
                           ((p - projected).square().sum(-1) + 1e-10))


@torch.no_grad()
def validate(model, sampler, criterion, batches: int, output: Path | None):
    model.eval()
    rng = np.random.default_rng(9152026)
    totals = {"loss": [], "overlap_si_sdri_db": [], "absent_attenuation_db": [], "clean_sdr_db": []}
    for index in range(batches):
        kind = ["overlap", "absent", "clean"][index % 3]
        x, target, _ = sampler.batch(rng, 1, forced_kind=kind)
        y, _ = model(x)
        n = min(x.shape[-1], target.shape[-1], y.shape[-1])
        x, target, y = x[..., :n], target[..., :n], y[..., :n]
        value = criterion(y, target, x)
        if not torch.isfinite(value):
            raise RuntimeError("验证损失包含无效值。")
        totals["loss"].append(float(value))
        if kind == "overlap":
            totals["overlap_si_sdri_db"].append(float((si_sdr(y, target) - si_sdr(x, target)).mean()))
        elif kind == "absent":
            totals["absent_attenuation_db"].append(float(10 * torch.log10(
                (x.square().mean() + 1e-10) / (y.square().mean() + 1e-10))))
        else:
            totals["clean_sdr_db"].append(float(10 * torch.log10(
                (target.square().mean() + 1e-10) / ((y - target).square().mean() + 1e-10))))
        if output is not None and index < 3:
            output.mkdir(parents=True, exist_ok=True)
            for name, audio in [("mixture", x), ("target", target), ("output", y)]:
                sf.write(output / f"{kind}_{name}.wav", audio[0].cpu().numpy(), SR, subtype="FLOAT")
    return {key: float(np.mean(values)) for key, values in totals.items() if values}


def main():
    parser = argparse.ArgumentParser(description="48kHz个人目标人声过滤模型微调")
    parser.add_argument("--epochs", type=int, default=10)
    parser.add_argument("--steps-per-epoch", type=int, default=240)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--seconds", type=float, default=2.0)
    parser.add_argument("--lr", type=float, default=3e-5)
    parser.add_argument("--validation-batches", type=int, default=24)
    parser.add_argument("--resume", type=Path)
    args = parser.parse_args()
    if not torch.cuda.is_available():
        raise RuntimeError("没有检测到 CUDA GPU，未启动耗时的 CPU 训练。")
    torch.set_num_threads(4)
    torch.backends.cudnn.benchmark = True
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.manual_seed(42131)
    np.random.seed(42131)
    random.seed(42131)
    device = torch.device("cuda:0")
    dataset = json.loads((BASE / "dataset.json").read_text(encoding="utf-8"))
    rows = dataset["files"]
    run = (args.resume.parent if args.resume else
           BASE / "runs" / datetime.now(timezone(timedelta(hours=8))).strftime("personal48k_%Y%m%d_%H%M%S"))
    run.mkdir(parents=True, exist_ok=args.resume is not None)
    atomic_json(BASE / "latest_run.json", {"directory": str(run), "pid": os.getpid(), "started_at": now()})
    status_file = run / "status.json"
    status = {"state": "initializing", "pid": os.getpid(), "sample_rate": SR,
              "started_at": now(), "run": str(run), "gpu": torch.cuda.get_device_name(0),
              "epoch": 0, "step": 0, "note": "实验性固定个人模型；尚未通过直播音质和多人实录验收。"}
    atomic_json(status_file, status)
    atomic_json(run / "config.json", vars(args) | {"resume": str(args.resume) if args.resume else None,
        "sample_rate": SR, "model": "DPDFNet48HR_personal_finetune", "pretrained": "Ceva-IP/DPDFNet",
        "dataset": str(BASE / "dataset.json"), "target": "user_only_fixed_identity",
        "dataset_limitations": dataset["limitations"], "batch_norm_running_stats": "frozen",
        "loss": "scale_dependent_waveform_and_multiresolution_fullband_spectral",
        "stop": "Create STOP file in the run directory; state is saved after the current batch."})
    # Windows kernel file lock releases automatically after crashes; a second
    # invocation cannot silently compete for the GPU while this process runs.
    lock = (BASE / "training.lock").open("a+b")
    if lock.tell() == 0:
        lock.write(b"0")
        lock.flush()
    lock.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    step = 0
    try:
        model = build_model(device)
        optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-5)
        if args.resume:
            checkpoint = torch.load(args.resume, map_location=device, weights_only=True)
            model.load_state_dict(checkpoint["model"])
            optimizer.load_state_dict(checkpoint["optimizer"])
            step = int(checkpoint["step"])
        else:
            weights = torch.load(BASE / "pretrained" / "dpdfnet2_48khz_hr.pth", map_location="cpu", weights_only=True)
            model.load_state_dict(weights, strict=True)
        sampler = Mixtures(rows, "train", args.seconds, device)
        validation = Mixtures(rows, "validation", args.seconds, device)
        criterion = FidelityLoss(device)
        print("TRAINING_GPU", status["gpu"], "SAMPLE_RATE", SR, "PARAMETERS", sum(p.numel() for p in model.parameters()), flush=True)
        print("RUN", run, flush=True)
        print("BASELINE_VALIDATION", flush=True)
        baseline = validate(model, validation, criterion, args.validation_batches, run / "baseline_audio")
        atomic_json(run / "baseline.json", baseline)
        print("BASELINE", json.dumps(baseline), flush=True)
        best_loss = math.inf
        rng = np.random.default_rng(42131 + step)
        begun = time.perf_counter()
        loss_sum = 0.0
        stopped = False
        for epoch in range(1, args.epochs + 1):
            model.train()
            for module in model.modules():
                if isinstance(module, nn.modules.batchnorm._BatchNorm):
                    module.eval()
            for local_step in range(1, args.steps_per_epoch + 1):
                if (run / "STOP").exists():
                    stopped = True
                    break
                step += 1
                x, target, kinds = sampler.batch(rng, args.batch_size)
                optimizer.zero_grad(set_to_none=True)
                y, _ = model(x)
                n = min(y.shape[-1], target.shape[-1])
                loss = criterion(y[..., :n], target[..., :n], x[..., :n])
                if not torch.isfinite(loss):
                    raise RuntimeError(f"第{step}步损失异常，已停止以免保存损坏模型。")
                loss.backward()
                norm = torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
                if not torch.isfinite(norm):
                    raise RuntimeError(f"第{step}步梯度异常。")
                progress = min(1., step / (args.epochs * args.steps_per_epoch))
                lr = args.lr * min(1., step / 40) * (.15 + .85 * (1 + math.cos(math.pi * progress)) / 2)
                for group in optimizer.param_groups:
                    group["lr"] = lr
                optimizer.step()
                loss_sum += float(loss.detach())
                if step % 10 == 0 or step == 1:
                    elapsed = time.perf_counter() - begun
                    status.update(state="training", epoch=epoch, step=step,
                        total_steps=args.epochs * args.steps_per_epoch,
                        last_loss=float(loss.detach()), lr=lr,
                        elapsed_seconds=round(elapsed, 1), updated_at=now(),
                        gpu_allocated_mib=round(torch.cuda.memory_allocated() / 1024**2))
                    atomic_json(status_file, status)
                    print(f"STEP {step}/{args.epochs * args.steps_per_epoch} epoch={epoch} loss={float(loss.detach()):.5f} lr={lr:.7f} elapsed={elapsed:.1f}s", flush=True)
                if step == 25 or step % 120 == 0:
                    payload = {"model": model.state_dict(), "optimizer": optimizer.state_dict(),
                               "step": step, "epoch": epoch, "sample_rate": SR, "model_type": "DPDFNet48HR",
                               "target_identity": "fixed_user", "pretrained_source": "Ceva-IP/DPDFNet"}
                    save_checkpoint(run / "last.pt", payload)
            payload = {"model": model.state_dict(), "optimizer": optimizer.state_dict(),
                       "step": step, "epoch": epoch, "sample_rate": SR, "model_type": "DPDFNet48HR",
                       "target_identity": "fixed_user", "pretrained_source": "Ceva-IP/DPDFNet"}
            save_checkpoint(run / "last.pt", payload)
            if stopped:
                break
            metrics = validate(model, validation, criterion, args.validation_batches, run / "latest_audio")
            metrics.update(epoch=epoch, step=step, updated_at=now(), train_loss=loss_sum / args.steps_per_epoch)
            loss_sum = 0.
            with (run / "metrics.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(metrics) + "\n")
            print("VALIDATION", json.dumps(metrics), flush=True)
            if metrics["loss"] < best_loss:
                best_loss = metrics["loss"]
                save_checkpoint(run / "best.pt", payload)
                save_checkpoint(run / "best_weights.pth", model.state_dict())
                atomic_json(run / "best_metrics.json", metrics)
            status.update(best_validation_loss=best_loss, validation=metrics)
        status.update(state="stopped" if stopped else "completed", step=step, finished_at=now())
        atomic_json(status_file, status)
        print("TRAINING_STOPPED" if stopped else "TRAINING_COMPLETED", "steps", step, "run", run, flush=True)
    except BaseException as exc:
        status.update(state="failed", step=step, error=str(exc), updated_at=now())
        atomic_json(status_file, status)
        traceback.print_exc()
        raise
    finally:
        lock.close()


if __name__ == "__main__":
    main()
