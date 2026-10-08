"""Pre-fetch the models LiveTranslate needs, without going through the GUI wizard.

The first-launch wizard and the auto-download dialog both need a human to click
through them; this script does the same work headlessly so an install can be
prepared (or verified in CI) without a display.

Usage (project env python):

    D:/DevVmEnv/Anaconda_envs/envs/livetranslate/python.exe doc/migrations/prefetch_models.py

Options via env:
    LT_HUB=ms|hf        model hub (default ms -- ModelScope, fast in China)
    LT_FUNASR_MODEL     funasr model key (default sensevoice-small)
    LT_PROXY=none|system|<url>   (default none -- domestic ModelScope, avoids
                                 a flaky ambient proxy)
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

HUB = os.environ.get("LT_HUB", "ms")
FUNASR_MODEL = os.environ.get("LT_FUNASR_MODEL", "sensevoice-small")
PROXY = os.environ.get("LT_PROXY", "none")


def main() -> int:
    from livetranslate.models.manager import (
        apply_cache_env,
        download_asr,
        download_silero,
        format_size,
        get_missing_models,
        is_asr_cached,
        is_silero_cached,
    )

    apply_cache_env()

    print(f"hub={HUB} funasr_model={FUNASR_MODEL} proxy={PROXY}")
    print(f"silero cached : {is_silero_cached()}")
    print(f"asr cached    : {is_asr_cached('funasr', FUNASR_MODEL, HUB)}")

    missing = get_missing_models("funasr", FUNASR_MODEL, HUB)
    if not missing:
        print("nothing to download")
        return 0

    for item in missing:
        print(f"  needs: {item['name']} (~{format_size(item['estimated_bytes'])})")

    t0 = time.time()
    # silero-vad ships the model in its wheel, so this is normally a no-op
    download_silero(proxy=PROXY)
    download_asr("funasr", FUNASR_MODEL, hub=HUB, proxy=PROXY)
    print(f"download finished in {time.time() - t0:.0f}s")

    still = get_missing_models("funasr", FUNASR_MODEL, HUB)
    if still:
        print("STILL MISSING:")
        for item in still:
            print(f"  - {item['name']}")
        return 1
    print("all models ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
