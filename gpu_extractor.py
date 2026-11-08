from __future__ import annotations

import onnxruntime as ort
import voxlock


class CudaVoxLock(voxlock.VoxLock):
    """The original FP32 network and streaming state, using CUDA kernels."""

    def __init__(self):
        import torch
        if "CUDAExecutionProvider" not in ort.get_available_providers():
            raise RuntimeError("当前 ONNX Runtime 未启用 CUDA。")
        from huggingface_hub import hf_hub_download
        from voxlock.core import REPO, REVISION
        try:
            paths = [hf_hub_download(REPO, f"onnx/v2/{name}.onnx", revision=REVISION,
                                     local_files_only=True) for name in ("enroll", "chunk")]
        except FileNotFoundError:
            root = voxlock.download("fp32")
            paths = [root / "enroll.onnx", root / "chunk.onnx"]
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        providers = [("CUDAExecutionProvider", {"use_tf32": 0, "do_copy_in_default_stream": 1,
                      "arena_extend_strategy": "kSameAsRequested", "gpu_mem_limit": 512 * 1024 * 1024}),
                     "CPUExecutionProvider"]
        self.enroll = ort.InferenceSession(str(paths[0]), options, providers=providers)
        self.chunk = ort.InferenceSession(str(paths[1]), options, providers=providers)
        if "CUDAExecutionProvider" not in self.chunk.get_providers():
            raise RuntimeError("声纹模型未能加载 CUDA 执行器。")
        self.state = tuple({item.name: item.shape for item in self.chunk.get_inputs()}["h"])
