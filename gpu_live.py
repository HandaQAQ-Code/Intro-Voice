"""Use the installed CUDA PyTorch with the project's audio dependencies."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / ".venv" / "Lib" / "site-packages"))
CUDA_RUNTIME = ROOT / "data" / "runtime" / "onnx-cuda"
if (CUDA_RUNTIME / "onnxruntime").is_dir():
    sys.path.insert(0, str(CUDA_RUNTIME))

if __name__ == "__main__":
    try:
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError("未检测到可用的 CUDA 版 PyTorch。")
        print(f"CUDA: {torch.cuda.get_device_name(0)} | PyTorch {torch.__version__}", flush=True)
        import live_voice
        sys.argv[1:1] = ["--gpu"]
        live_voice.main()
    except Exception as error:
        print(f"GPU 启动失败：{error}", file=sys.stderr)
        print(r"缺少依赖时运行：.venv\Scripts\python.exe -m pip install -r requirements-gpu.txt",
              file=sys.stderr)
        raise
