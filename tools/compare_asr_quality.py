"""Side-by-side quality comparison: sherpa-onnx int8 vs FunASR on real audio.

Latency is measured by ``bench_asr_backends.py``; this script answers the other
half of the question -- *is the CPU path's text as good?* The two are compared
on identical clips, and both raw outputs are printed so differences are visible
rather than summarised into a score.

There is no ground truth for a class recording, so this deliberately prints
both transcripts instead of computing a CER against one of them: the point is
to let a human see whether sherpa drops characters or mislabels the language,
not to produce a number that would just be measuring the other backend.

Import order matters: librosa pulls ``scipy.signal`` lazily, and
``funasr_boot`` stubs scipy out before importing funasr. Load all audio first.

Run with the project env python::

    python tools/compare_asr_quality.py --audio <file.mp3> --offsets 60,600,1800
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FUNASR_DIR = ROOT / "models/modelscope/models/iic--SenseVoiceSmall/snapshots/master"
SHERPA_DIR = ROOT / "models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
PUNCT = (
    ROOT
    / "models/sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8"
    / "model.int8.onnx"
)
EXAMPLE = FUNASR_DIR / "example/zh.mp3"


def load_clips(src: Path, offsets: list[float], duration: float):
    import librosa
    import numpy as np

    clips = []
    for off in offsets:
        y, _ = librosa.load(
            str(src), sr=16000, mono=True, offset=off or None, duration=duration
        )
        clips.append(np.ascontiguousarray(y, dtype=np.float32))
    return clips


def build_funasr(device: str):
    from livetranslate.models.manager import apply_cache_env

    apply_cache_env()
    from livetranslate.asr.funasr_boot import import_auto_model

    AutoModel = import_auto_model()
    return AutoModel(
        model=str(FUNASR_DIR),
        trust_remote_code=True,
        device=device,
        hub="ms",
        disable_update=True,
    )


def build_sherpa(threads: int):
    import sherpa_onnx

    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(SHERPA_DIR / "model.int8.onnx"),
        tokens=str(SHERPA_DIR / "tokens.txt"),
        num_threads=threads,
        use_itn=True,
        language="",
    )
    punctuation = None
    if PUNCT.is_file():
        punctuation = sherpa_onnx.OfflinePunctuation(
            sherpa_onnx.OfflinePunctuationConfig(
                model=sherpa_onnx.OfflinePunctuationModelConfig(
                    ct_transformer=str(PUNCT), num_threads=1, provider="cpu"
                )
            )
        )
    return recognizer, punctuation


def run_funasr(model, audio):
    result = model.generate(
        input=audio,
        cache={},
        language="auto",
        use_itn=True,
        batch_size_s=0,
        disable_pbar=True,
    )
    return result[0]["text"] if result else ""


def run_sherpa(recognizer, punctuation, audio):
    stream = recognizer.create_stream()
    stream.accept_waveform(16000, audio)
    recognizer.decode_stream(stream)
    raw = stream.result.text
    lang = stream.result.lang
    text = punctuation.add_punctuation(raw) if punctuation else raw
    return raw, text, lang


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", default=str(EXAMPLE))
    ap.add_argument("--offsets", default="0", help="comma-separated sample points")
    ap.add_argument("--duration", type=float, default=8.0)
    ap.add_argument("--funasr-device", default="cpu", help="cpu or cuda")
    ap.add_argument("--threads", type=int, default=4)
    args = ap.parse_args()

    src = Path(args.audio)
    if not src.is_file():
        print(f"FAIL: audio not found: {src}")
        return 1

    offsets = [float(x) for x in args.offsets.split(",") if x.strip()]
    clips = load_clips(src, offsets, args.duration)
    print(f"loaded {len(clips)} clip(s) of {args.duration:g}s from {src.name}\n")

    print("building funasr...")
    funasr = build_funasr(args.funasr_device)
    print("building sherpa-onnx...")
    recognizer, punctuation = build_sherpa(args.threads)
    print()

    # Both halves of the comparison, printed verbatim. Diffs are the deliverable
    # here, so nothing is normalised or scored.
    for off, clip in zip(offsets, clips):
        print("=" * 78)
        print(f"  @{off:g}s  ({len(clip) / 16000:.2f}s)")
        print("=" * 78)
        fa = run_funasr(funasr, clip)
        raw, sh, lang = run_sherpa(recognizer, punctuation, clip)
        print(f"  funasr    : {fa}")
        print(f"  sherpa raw: {raw}")
        print(f"  sherpa+pn : {sh}")
        print(f"  sherpa lang: {lang}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
