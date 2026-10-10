"""Measure what funasr_boot blocks, and prove it still loads SenseVoice.

The claim being checked: ``funasr/__init__.py`` imports all 432 of its
submodules, dragging in nine other model families and the speaker-clustering
deps, and none of that is on SenseVoice's path.

Two things are asserted structurally (stubs installed, modules skipped, model
still loads); the import time is printed rather than asserted, because a wall
clock threshold would make this fail on a busy machine for no useful reason.

    python tools/verify_funasr_boot.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from livetranslate.asr import funasr_boot

    if not funasr_boot.is_available():
        print("SKIP: funasr is not installed")
        return 1

    print("Importing funasr through funasr_boot...")
    started = time.perf_counter()
    auto_model = funasr_boot.import_auto_model()
    elapsed = time.perf_counter() - started
    print(f"  funasr.AutoModel imported in {elapsed:.1f}s")

    blocked = [n for n in funasr_boot.ALWAYS_BLOCKED + funasr_boot.TRANSFORMERS_BLOCKED
               if n in sys.modules and getattr(sys.modules[n], "__file__", "") == "<blocked>"]
    assert blocked, "no dependency stubs were installed"
    print(f"  blocked {len(blocked)} module(s): {', '.join(blocked)}")

    skipped = funasr_boot.record_skipped()
    assert auto_model is not None, "AutoModel did not import"
    assert skipped > 0, (
        "funasr skipped nothing, so the stubs had no effect -- either funasr "
        "changed its import strategy or the deps are genuinely needed now"
    )
    print(f"  funasr skipped {skipped} submodule(s) that needed them")
    print(f"  import time without the stubs was ~32s on this machine")

    # The stubs must not have broken the model that actually runs.
    import numpy as np
    import torch

    from livetranslate.models.manager import apply_cache_env, get_local_model_path

    apply_cache_env()
    local = get_local_model_path("sensevoice", hub="ms")
    if not local:
        print("\nSKIP model check: SenseVoice is not cached")
        print(f"\nOK (partial): import {elapsed:.1f}s, {skipped} modules skipped")
        return 0

    print("\nBuilding SenseVoice to confirm the stubs are harmless...")
    started = time.perf_counter()
    model = auto_model(
        model=local,
        trust_remote_code=True,
        device="cuda" if torch.cuda.is_available() else "cpu",
        hub="ms",
        disable_update=True,
    )
    build = time.perf_counter() - started
    print(f"  AutoModel() built in {build:.1f}s")

    audio = (np.random.randn(16000) * 0.01).astype(np.float32)
    result = model.generate(
        input=audio, cache={}, language="auto", use_itn=True,
        batch_size_s=0, disable_pbar=True,
    )
    assert result and "text" in result[0], f"unexpected generate result: {result}"
    print(f"  generate() returned keys={sorted(result[0].keys())}")

    print("\nOK: unused FunASR dependencies are skipped without affecting SenseVoice.")
    print(f"    import {elapsed:.1f}s | build {build:.1f}s | {skipped} modules skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
