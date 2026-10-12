"""Verify the ONNX VAD backend matches the torch one, and what it costs.

Switching the VAD from torch to onnxruntime is only safe if it changes nothing
but the runtime. Per-window probabilities were already checked to match to
2e-6, but VADProcessor layers a lot on top of that number (progressive silence,
adaptive pause thresholds, best-pause splitting), so this drives the whole
processor through both runtimes on the same real audio and compares the
segments it emits -- plus the per-chunk cost, since this runs on the capture
thread once every 32ms.

Run with the project env python::

    python tools/verify_vad_onnx.py [--audio <file.mp3>] [--offset 60]
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

import librosa
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_AUDIO = (
    ROOT / "models/modelscope/models/iic--SenseVoiceSmall/snapshots/master/example/zh.mp3"
)
CHUNK = 512
BUDGET_MS = 32.0  # one chunk is 32ms of audio


def drive(runtime: str, audio: np.ndarray) -> dict:
    """Run VADProcessor with `runtime` forced, returning segments and timings."""
    from livetranslate.audio.vad import VADProcessor

    vad = VADProcessor(
        sample_rate=16000,
        threshold=0.5,
        min_speech_duration=1.0,
        max_speech_duration=8.0,
    )
    if runtime == "torch":
        # The processor picked ONNX at construction; swap the model afterwards.
        from silero_vad import load_silero_vad

        model = load_silero_vad()
        model.eval()
        vad._model = model
        vad._vad_runtime = "torch"
    elif vad._vad_runtime != "onnx":
        raise AssertionError(f"expected the ONNX path, got {vad._vad_runtime!r}")

    segments: list[float] = []
    times: list[float] = []
    for i in range(0, len(audio) - CHUNK + 1, CHUNK):
        t0 = time.perf_counter()
        out = vad.process_chunk(audio[i : i + CHUNK])
        times.append(time.perf_counter() - t0)
        if out is not None:
            segments.append(len(out) / 16000)
    tail = vad.flush()
    if tail is not None:
        segments.append(len(tail) / 16000)

    return {
        "runtime": runtime,
        "segments": segments,
        "median_ms": statistics.median(times) * 1000,
        "p95_ms": sorted(times)[int(len(times) * 0.95)] * 1000,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", default=str(DEFAULT_AUDIO))
    ap.add_argument("--offset", type=float, default=0.0)
    ap.add_argument("--duration", type=float, default=30.0)
    args = ap.parse_args()

    src = Path(args.audio)
    if not src.is_file():
        print(f"FAIL: audio not found: {src}")
        return 1

    y, _ = librosa.load(
        str(src), sr=16000, mono=True, offset=args.offset or None,
        duration=args.duration,
    )
    audio = np.ascontiguousarray(y, dtype=np.float32)
    n_chunks = (len(audio) - CHUNK) // CHUNK + 1
    print(f"audio: {src.name}  {len(audio) / 16000:.1f}s  ({n_chunks} chunks of 32ms)\n")

    results: dict[str, dict] = {}
    for runtime in ("onnx", "torch"):
        try:
            results[runtime] = drive(runtime, audio)
        except Exception as exc:
            print(f"[{runtime}] unavailable: {type(exc).__name__}: {exc}")

    print(f"  {'runtime':<8} {'segs':>5} {'total s':>9} {'median ms':>11} {'p95 ms':>8}")
    for runtime, res in results.items():
        print(
            f"  {runtime:<8} {len(res['segments']):>5} "
            f"{sum(res['segments']):>9.1f} {res['median_ms']:>11.2f} "
            f"{res['p95_ms']:>8.2f}"
        )
    print()

    if "onnx" in results and "torch" in results:
        onnx_segs = [round(s, 2) for s in results["onnx"]["segments"]]
        torch_segs = [round(s, 2) for s in results["torch"]["segments"]]
        print(f"  onnx  segments: {onnx_segs}")
        print(f"  torch segments: {torch_segs}")
        print()
        if onnx_segs == torch_segs:
            print("  VERDICT: identical segmentation")
        else:
            print("  VERDICT: SEGMENTS DIFFER (probabilities matched to 2e-6, so this")
            print("           would mean the runtimes disagree about state, not audio)")
        print()

    for runtime, res in results.items():
        verdict = "OK" if res["median_ms"] < BUDGET_MS else "TOO SLOW"
        print(
            f"  {runtime:<8} median {res['median_ms']:.2f}ms vs "
            f"{BUDGET_MS:.0f}ms chunk budget -> {verdict}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
