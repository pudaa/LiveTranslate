"""End-to-end GPU check: load the real ASR backend on CUDA and transcribe.

Distinct from verify_runtime.py, which starts a worker with a deliberately
invalid model to prove the *spawn round-trip* works. This one loads the actual
SenseVoice model on the actual GPU and runs a real inference, which is the only
way to catch CUDA/DLL problems (a bad torch build, a driver mismatch, a missing
cudnn) -- those are invisible until the model touches the device.

Run with the project env python:

    D:/DevVmEnv/Anaconda_envs/envs/livetranslate/python.exe doc/migrations/check_gpu_asr.py

Exit code 0 = the full ASR path works on the GPU.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FUNASR_MODEL = "sensevoice-small"


def main() -> int:
    import numpy as np

    from livetranslate.models.manager import apply_cache_env, is_asr_cached

    apply_cache_env()

    import torch

    print(f"torch          : {torch.__version__}")
    print(f"cuda available : {torch.cuda.is_available()}")
    if not torch.cuda.is_available():
        print("FAIL: no CUDA device visible; ASR would fall back to CPU")
        return 1
    print(f"device         : {torch.cuda.get_device_name(0)}")
    print(f"capability     : {torch.cuda.get_device_capability(0)}")
    vram_gb = torch.cuda.get_device_properties(0).total_memory / 1024**3
    print(f"vram           : {vram_gb:.1f} GB")

    if not is_asr_cached("funasr", FUNASR_MODEL, "ms"):
        print(f"FAIL: {FUNASR_MODEL} is not cached; run prefetch_models.py first")
        return 1

    from livetranslate.asr.client import ASRClient, ASRClientError

    config = {
        "engine_type": "funasr",
        "funasr_model": FUNASR_MODEL,
        "device": "cuda",
        "hub": "ms",
        "language": "auto",
        "pad_seconds": 0.5,
        "display_name": FUNASR_MODEL,
    }

    client = ASRClient(config, ready_timeout=420.0, request_timeout=180.0)
    try:
        t0 = time.perf_counter()
        client.start()
        ready = client.wait_ready()
        print(f"worker ready   : {time.perf_counter() - t0:.1f}s  {ready}")

        # 1s of silence -- enough to prove the model runs on the device end to
        # end. (An empty result is the correct answer for silence.)
        silence = np.zeros(16000, dtype=np.float32)
        t1 = time.perf_counter()
        result = client.transcribe(silence)
        print(f"transcribe(silence) -> {result!r}  in {(time.perf_counter() - t1) * 1000:.0f}ms")

        peak = torch.cuda.max_memory_allocated() / 1024**2
        # This is the *parent* process, which is expected to stay at ~0: the model
        # lives in the worker subprocess (that isolation is the point). A non-zero
        # value here would mean something leaked into the GUI process.
        print(f"parent gpu alloc: {peak:.0f} MB (expected ~0 -- model lives in the worker)")
        print("\nOK: ASR backend loaded and ran on the GPU")
        return 0
    except ASRClientError as exc:
        print(f"FAIL: worker error: {exc}")
        return 1
    finally:
        try:
            client.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
