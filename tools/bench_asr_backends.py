"""Benchmark ASR latency across backends -- GPU vs CPU vs ONNX Runtime.

Answers one question: can CPU inference keep up with the real-time subtitle
pipeline, or is the GPU genuinely required?

Design notes
------------
* Uses the *same* audio for every backend, so the numbers are comparable.
* Uses the real ``SenseVoiceEngine`` (not a hand-rolled model call) so the
  measurement includes the same padding / tag-stripping path production uses.
* Reports RTF = elapsed / audio_duration. RTF < 1 means faster than real time;
  the subtitle pipeline needs a comfortable margin below 1 (see the verdict
  table at the end of the run).
* Samples nvidia-smi at phase boundaries. This is what tells us whether the
  dGPU drops out of P0 when nothing touches CUDA -- the actual power question.

Run with the project env python::

    D:/DevVmEnv/Anaconda_envs/envs/livetranslate/python.exe tools/bench_asr_backends.py

Optional: pass ``--fast`` to skip the CPU fp32 run (it is slow by design).
"""

from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FUNASR_MODEL = "sensevoice-small"
EXAMPLE_AUDIO = (
    ROOT
    / "models/modelscope/models/iic--SenseVoiceSmall/snapshots/master/example/zh.mp3"
)
SHERPA_MODEL_DIR = ROOT / "models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"

NVIDIA_SMI = r"C:\Windows\System32\nvidia-smi.exe"


# ── helpers ────────────────────────────────────────────────────────────────


def gpu_state() -> dict:
    """P-state / power / util snapshot. The P-state is the power headline."""
    try:
        out = subprocess.check_output(
            [
                NVIDIA_SMI,
                "--query-gpu=pstate,power.draw,utilization.gpu,memory.used",
                "--format=csv,noheader,nounits",
            ],
            timeout=15,
            text=True,
        )
        pstate, power, util, mem = [x.strip() for x in out.strip().split(",")]
        return {
            "pstate": pstate,
            "power_w": power,
            "util_pct": util,
            "mem_mib": mem,
        }
    except Exception as exc:  # nvidia-smi missing / driver busy
        return {"error": f"{type(exc).__name__}: {exc}"}


def load_audio(source: Path, seconds: float | None = None, offset: float = 0.0):
    """16 kHz mono float32.

    ``seconds=None`` keeps the whole clip. Longer requests are tiled from the
    same real speech; ``offset`` skips the head (class recordings often start
    with bell / silence, which would flatter the numbers).
    """
    import librosa
    import numpy as np

    y, _ = librosa.load(
        str(source), sr=16000, mono=True, offset=offset if offset else None
    )
    if seconds is None:
        return np.asarray(y, dtype=np.float32)
    need = int(seconds * 16000)
    if len(y) < need:
        reps = need // len(y) + 1
        y = np.tile(y, reps)[:need].copy()
    else:
        y = y[:need].copy()
    return np.asarray(y, dtype=np.float32)


def bench_transcribe(engine, audio, warmup: int = 1, runs: int = 5) -> dict:
    """Warm up once (kernels / cache), then time `runs` iterations."""
    import numpy as np

    for _ in range(warmup):
        engine.transcribe(np.ascontiguousarray(audio))

    times: list[float] = []
    text = ""
    for _ in range(runs):
        t0 = time.perf_counter()
        result = engine.transcribe(np.ascontiguousarray(audio))
        times.append(time.perf_counter() - t0)
        if isinstance(result, dict) and result.get("text"):
            text = result["text"]

    dur = len(audio) / 16000
    med = statistics.median(times)
    return {
        "dur_s": dur,
        "min_ms": min(times) * 1000,
        "med_ms": med * 1000,
        "max_ms": max(times) * 1000,
        "rtf_med": med / dur,
        "rtf_min": min(times) / dur,
        "text": text,
    }


def report_row(label: str, r: dict) -> str:
    return (
        f"  {label:<26} {r['dur_s']:>5.2f}s  "
        f"min {r['min_ms']:>7.1f}ms  med {r['med_ms']:>7.1f}ms  "
        f"max {r['max_ms']:>7.1f}ms   RTF(med) {r['rtf_med']:.3f}"
    )


# ── backends ───────────────────────────────────────────────────────────────


def run_pytorch(device: str, clips: dict, num_threads: int | None = None) -> dict:
    """The project's real SenseVoiceEngine. CPU path runs fp32 (see _set_precision)."""
    import torch

    if num_threads:
        torch.set_num_threads(num_threads)

    from livetranslate.asr.backends.sensevoice import SenseVoiceEngine

    label = f"pytorch/{device}" + (f" (nthreads={num_threads})" if num_threads else "")
    print(f"\n[{label}] loading...")
    t0 = time.perf_counter()
    engine = SenseVoiceEngine(device=device, hub="ms", pad_seconds=0.5)
    load_s = time.perf_counter() - t0
    print(f"[{label}] loaded in {load_s:.1f}s   gpu={gpu_state()}")

    out = {}
    for name, audio in clips.items():
        r = bench_transcribe(engine, audio)
        out[name] = r
        print(report_row(f"{label} @ {name}", r))

    try:
        engine.unload()
    except Exception:
        pass
    return {"load_s": load_s, "clips": out}


def run_sherpa(clips: dict, thread_counts: list[int]) -> dict | None:
    """sherpa-onnx + SenseVoice int8 ONNX, one recognizer per thread count.

    Returns ``{nthreads: {"load_s":…, "clips":…}}`` or None when unavailable.
    Thread count matters for the power budget: more CPU threads finish sooner
    but raise the package power, which is the thing we are trying to lower.
    """
    try:
        import sherpa_onnx
    except ImportError:
        print("\n[sherpa-onnx] not installed -- skipping")
        return None

    model = SHERPA_MODEL_DIR / "model.int8.onnx"
    tokens = SHERPA_MODEL_DIR / "tokens.txt"
    if not model.is_file():
        print(f"\n[sherpa-onnx] model missing at {model} -- skipping")
        return None

    out: dict = {}
    for nt in thread_counts:
        label = f"sherpa-onnx/int8/cpu (nthreads={nt})"
        print(f"\n[{label}] loading...")
        t0 = time.perf_counter()
        recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model),
            tokens=str(tokens),
            num_threads=nt,
            use_itn=True,
            language="zh",
        )
        load_s = time.perf_counter() - t0
        print(f"[{label}] loaded in {load_s:.1f}s   gpu={gpu_state()}")

        per_clip = {}
        for name, audio in clips.items():
            # Warm up, then time decode_stream only. Stream setup is included
            # because production also builds one stream per request, but it is
            # microseconds either way.
            s = recognizer.create_stream()
            s.accept_waveform(16000, audio)
            recognizer.decode_stream(s)

            times = []
            text = ""
            for _ in range(5):
                t0 = time.perf_counter()
                s = recognizer.create_stream()
                s.accept_waveform(16000, audio)
                recognizer.decode_stream(s)
                times.append(time.perf_counter() - t0)
                text = s.result.text

            dur = len(audio) / 16000
            med = statistics.median(times)
            per_clip[name] = {
                "dur_s": dur,
                "min_ms": min(times) * 1000,
                "med_ms": med * 1000,
                "max_ms": max(times) * 1000,
                "rtf_med": med / dur,
                "rtf_min": min(times) / dur,
                "text": text,
            }
            print(report_row(f"{label} @ {name}", per_clip[name]))

        out[nt] = {"load_s": load_s, "clips": per_clip}

    return out


# ── main ───────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--only",
        choices=["all", "gpu", "cpu", "sherpa"],
        default="all",
        help="run a single backend instead of all of them",
    )
    ap.add_argument(
        "--audio",
        default=str(EXAMPLE_AUDIO),
        help="source audio; 2 / 5.6 / 8 s slices are taken from it",
    )
    ap.add_argument(
        "--offset",
        type=float,
        default=0.0,
        help="skip this many seconds into the source before slicing",
    )
    ap.add_argument(
        "--offsets",
        default="",
        help="comma-separated sample points, e.g. 300,1200,2400 (each takes "
        "--duration seconds starting there). Overrides the default 2/5.6/8 s set.",
    )
    ap.add_argument(
        "--duration",
        type=float,
        default=8.0,
        help="clip length used with --offsets",
    )
    ap.add_argument(
        "--threads",
        default="4",
        help="comma-separated CPU thread counts for sherpa-onnx, e.g. 2,4,8",
    )
    args = ap.parse_args()

    from livetranslate.models.manager import apply_cache_env, is_asr_cached

    apply_cache_env()

    src = Path(args.audio)
    print("=" * 78)
    print("  LiveTranslate ASR latency benchmark")
    print("=" * 78)
    print(f"  audio      : {src}")
    if args.offset:
        print(f"  offset     : {args.offset:.0f}s into the source")
    print(f"  gpu before : {gpu_state()}")

    if not src.is_file():
        print(f"FAIL: audio not found: {src}")
        return 1
    if not is_asr_cached("funasr", FUNASR_MODEL, "ms"):
        print("FAIL: sensevoice-small not cached; run tools/prefetch_models.py")
        return 1

    if args.offsets:
        offs = [float(x) for x in args.offsets.split(",") if x.strip()]
        clips = {
            f"@{int(o)}s/{args.duration:g}s": load_audio(src, args.duration, o)
            for o in offs
        }
    else:
        clips = {
            "2.0s": load_audio(src, 2.0, args.offset),
            "5.6s": load_audio(src, 5.6, args.offset),
            "8.0s": load_audio(src, 8.0, args.offset),
        }
    print(f"  clips      : {', '.join(clips)}")

    results: dict = {}

    if args.only in ("all", "gpu"):
        results["pytorch/cuda/fp16"] = run_pytorch("cuda", clips)
        print(f"  gpu after GPU run: {gpu_state()}")

    if args.only in ("all", "cpu"):
        results["pytorch/cpu/fp32"] = run_pytorch("cpu", clips)
        print(f"  gpu after CPU run: {gpu_state()}")

    if args.only in ("all", "sherpa"):
        nts = [int(x) for x in args.threads.split(",") if x.strip()]
        sherpa = run_sherpa(clips, nts)
        if sherpa:
            for nt, res in sherpa.items():
                results[f"sherpa/int8/nt={nt}"] = res
        print(f"  gpu after sherpa run: {gpu_state()}")

    # ── verdict ────────────────────────────────────────────────────────────
    keys = list(clips.keys())
    print("\n" + "=" * 78)
    print("  RTF summary (lower is better; <1.0 = faster than real time)")
    print("=" * 78)
    hdr = "".join(f"{k:>11}" for k in keys)
    print(f"  {'backend':<26}{hdr}{'last ms':>9}")
    for key, res in results.items():
        if not res:
            continue
        row = res["clips"]
        cells = "".join(f"{row[c]['rtf_med']:>11.3f}" for c in keys)
        last = keys[-1]
        print(f"  {key:<26}{cells}{row[last]['med_ms']:>9.0f}")

    print("\n  Recognised text (sanity-check that quality holds):")
    for key, res in results.items():
        if not res:
            continue
        for c in keys:
            txt = res["clips"][c]["text"]
            print(f"    {key:<26} {c:<12} {txt[:50]}")

    print("\n  Pipeline timing constraints (from user_settings.json / config.yaml):")
    print("    interim_interval        = 0.6 s   (how often a partial is requested)")
    print("    max_speech_duration     = 8.0 s   (longest VAD segment sent to ASR)")
    print("    min_speech_duration     = 1.0 s")
    print("  A backend must process an 8 s segment well inside 0.6 s to avoid")
    print("  the queue growing without bound.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
