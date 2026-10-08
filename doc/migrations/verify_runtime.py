"""Runtime verification for the P1/P2 refactor.

Static checks (``verify_p1.py``) prove the imports resolve. This script proves
the things only a live interpreter can:

  1. every module in the package imports cleanly with the real dependency set
  2. ``livetranslate.paths`` resolves to the actual project root -- the 11
     anchors that used to be ``Path(__file__).parent``
  3. the ASR registry resolves every engine key, and importing the registry does
     *not* drag torch into the process (the GUI-process invariant)
  4. the ASR worker really survives a ``spawn`` round-trip: the child re-imports
     the package under a new module name, which is where a botched package move
     shows up
  5. pipeline mode normalisation and the cinema style presets are wired

Run with the project env (do NOT use `conda run`, it eats stdin):

    D:/DevVmEnv/Anaconda_envs/envs/livetranslate/python.exe doc/migrations/verify_runtime.py

Exit code 0 = all checks passed.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, fn) -> None:
    try:
        detail = fn()
        PASS.append(f"{name}{f' -- {detail}' if detail else ''}")
    except Exception as exc:  # noqa: BLE001
        FAIL.append(f"{name}: {type(exc).__name__}: {exc}")
        traceback.print_exc()


# ── 1. import every module ───────────────────────────────────────────────────

MODULES = [
    "livetranslate",
    "livetranslate.paths",
    "livetranslate.benchmark",
    "livetranslate.audio.capture",
    "livetranslate.audio.vad",
    "livetranslate.asr.registry",
    "livetranslate.asr.client",
    "livetranslate.asr.worker",
    "livetranslate.asr.backends.whisper",
    "livetranslate.asr.backends.funasr",
    "livetranslate.asr.backends.sensevoice",
    "livetranslate.asr.backends.funasr_nano",
    "livetranslate.asr.backends.anime_whisper",
    "livetranslate.asr.backends.remote",
    "livetranslate.translate.translator",
    "livetranslate.translate.modes",
    "livetranslate.output.transcript",
    "livetranslate.models.manager",
]


def _import_torch_first():
    # Windows: torch must load before PyQt6 (PyTorch 2.9+ DLL conflict).
    import torch  # noqa: F401


def _import_all():
    import importlib

    _import_torch_first()
    for m in MODULES:
        importlib.import_module(m)
    # UI modules construct nothing at import time
    for m in [
        "livetranslate.ui.i18n",
        "livetranslate.ui.overlay",
        "livetranslate.ui.styles",
        "livetranslate.ui.subtitle_window",
        "livetranslate.ui.subtitle_settings",
        "livetranslate.ui.control_panel",
        "livetranslate.ui.dialogs",
        "livetranslate.ui.log_window",
    ]:
        importlib.import_module(m)
    return f"{len(MODULES)} core + 8 ui modules"


def _paths():
    from livetranslate import paths

    assert paths.APP_ROOT == ROOT, f"APP_ROOT={paths.APP_ROOT} expected {ROOT}"
    for name in ("CONFIG_FILE", "I18N_DIR", "MODELS_DIR", "LOGS_DIR", "TRANSCRIPTS_DIR"):
        p = getattr(paths, name)
        assert str(p).startswith(str(ROOT)), f"{name}={p} escapes the project root"
    assert paths.CONFIG_FILE.is_file(), "config.yaml not found via CONFIG_FILE"
    assert paths.I18N_DIR.is_dir(), "i18n/ not found via I18N_DIR"
    assert paths.MODELS_DIR == ROOT / "models", f"MODELS_DIR wrong: {paths.MODELS_DIR}"
    return f"APP_ROOT={paths.APP_ROOT}"


def _manager_paths():
    from livetranslate.models.manager import APP_DIR, MODELS_DIR

    assert APP_DIR == ROOT, f"manager.APP_DIR={APP_DIR}"
    assert MODELS_DIR == ROOT / "models", f"manager.MODELS_DIR={MODELS_DIR}"
    return "APP_DIR / MODELS_DIR point at the project root"


# ── 3. registry ──────────────────────────────────────────────────────────────

REGISTRY_SNIPPET = r"""
import sys
# Import the registry in a clean interpreter: it must NOT pull torch.
import livetranslate.asr.registry as r
assert "torch" not in sys.modules, "importing the registry imported torch"
for key in r.known_backends():
    mod, factory = r.resolve(key)
    print(f"{key} -> {mod}.{factory}")
assert r.resolve(None) == r.resolve(r.DEFAULT_BACKEND)
assert r.resolve("totally-bogus")[0].endswith("backends.whisper"), "fallback changed"
assert r.parse_device("cuda:0 (RTX 4060)") == ("cuda", 0)
assert r.parse_device("cuda") == ("cuda", 0)
assert r.parse_device("cpu") == ("cpu", 0)
assert r.parse_device(None) == ("cpu", 0)
# each backend must expose build_engine
import importlib
for key in r.known_backends():
    mod_name, _ = r.resolve(key)
    m = importlib.import_module(mod_name)
    assert hasattr(m, "build_engine"), f"{mod_name} has no build_engine"
print("lazy-import OK, all backends expose build_engine")
"""


def _registry_lazy():
    out = subprocess.run(
        [sys.executable, "-c", REGISTRY_SNIPPET],
        cwd=str(ROOT), capture_output=True, text=True, timeout=180,
    )
    if out.returncode != 0:
        raise AssertionError(f"clean-interpreter registry check failed:\n{out.stdout}\n{out.stderr}")
    return out.stdout.strip().splitlines()[-1]


# ── 4. spawn round-trip ──────────────────────────────────────────────────────

def _worker_spawn():
    """Start a real ASR worker subprocess with a deliberately invalid model.

    A clean, structured failure proves the spawn round-trip works: the child
    re-imports livetranslate.asr.worker + the registry + the whisper backend
    under a fresh module name (that is where a bad package move surfaces).
    """
    from livetranslate.asr.client import ASRClient, ASRClientError

    tmp = tempfile.mkdtemp(prefix="lt-worker-check-")
    config = {
        "engine_type": "whisper",
        "model_size": "this-model-does-not-exist",
        "device": "cpu",
        "compute_type": "int8",
        "language": "auto",
        "download_root": tmp,
        "hub": "ms",
    }
    client = ASRClient(config, ready_timeout=240.0)
    try:
        client.start()
        client.wait_ready()
    except ASRClientError as exc:
        # Expected: the model cannot load. What matters is that we got a real
        # response from the child rather than a hang/traceback in the parent.
        msg = str(exc)
        assert "ModuleNotFoundError" not in msg, f"child failed to import the package: {msg}"
        assert "ImportError" not in msg, f"child failed to import the package: {msg}"
        return f"child responded (expected load failure): {msg[:90]}"
    finally:
        try:
            client.shutdown()
        except Exception:
            pass
    raise AssertionError("bogus model unexpectedly loaded")


# ── 5. modes + presets ───────────────────────────────────────────────────────

def _modes():
    from livetranslate.translate import modes

    assert modes.normalize(None) == modes.TRANSLATE
    assert modes.normalize("bogus") == modes.TRANSLATE
    assert modes.normalize("captions") == "captions"
    assert modes.is_captions("captions") and not modes.is_captions("translate")
    return f"modes={modes.MODES} default={modes.DEFAULT}"


def _presets():
    from livetranslate.ui.overlay import DEFAULT_STYLE, STYLE_PRESETS
    from livetranslate.ui.styles import (
        CINEMA_OVERLAY_ITEMS,
        CINEMA_SUBWIN_ITEMS,
        CINEMA_SUBWIN_PRESETS,
        CINEMA_OVERLAY_PRESETS,
    )

    assert "show_meta" in DEFAULT_STYLE
    for key, _ in CINEMA_OVERLAY_ITEMS:
        assert key in STYLE_PRESETS, f"overlay preset {key} not merged into STYLE_PRESETS"
        assert STYLE_PRESETS[key].get("show_meta") is False, f"{key} should enable pure mode"
        # every key the style tab reads must be present once merged with DEFAULT_STYLE
        merged = {**DEFAULT_STYLE, **STYLE_PRESETS[key]}
        for field in ("bg_color", "bg_opacity", "header_color", "header_opacity",
                      "original_font_family", "translation_font_family",
                      "original_color", "translation_color", "timestamp_color"):
            assert field in merged, f"{key} missing {field}"
    for key, _ in CINEMA_SUBWIN_ITEMS:
        assert key in CINEMA_SUBWIN_PRESETS, f"subwin preset {key} missing"
        p = CINEMA_SUBWIN_PRESETS[key]
        assert len(p["lines"]) == 2, f"{key} must style both lines"
        for line in p["lines"]:
            assert "outline_width" in line and "font_size" in line and "color" in line
    return (f"{len(CINEMA_OVERLAY_ITEMS)} overlay + {len(CINEMA_SUBWIN_ITEMS)} "
            f"subtitle-window cinema presets")


def _pipeline_srt():
    """Drive the real pipeline dispatch and assert captions mode + SRT export.

    This is the fork's core promise in one check: with pipeline.mode=captions the
    translator must never be submitted to, and the recognised text must still land
    in an .srt with audio-clock cue times.
    """
    import tempfile

    import yaml

    from livetranslate.output.srt import SRTWriter
    from livetranslate.output.transcript import TranscriptWriter
    from livetranslate.paths import CONFIG_FILE
    from livetranslate.translate import modes
    from main import LiveTranslateApp

    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    cfg.setdefault("pipeline", {})["mode"] = "captions"

    app = LiveTranslateApp(cfg)
    assert app._pipeline_mode == modes.CAPTIONS, app._pipeline_mode
    assert app._srt.is_enabled(), "SRT export should default to on"

    tmp = Path(tempfile.mkdtemp(prefix="lt-srt-check-"))
    app._transcript = TranscriptWriter(tmp)
    app._srt = SRTWriter(tmp, enabled=True)

    submitted = []
    app._tl_executor.submit = lambda *a, **k: submitted.append(a)

    app._audio_clock_s = 10.0
    app._process_segment_text("第一句中文字幕。", "zh", 120.0, t_end=3.0)
    app._audio_clock_s = 16.0
    app._process_segment_text("第二句中文字幕。", "zh", 110.0, t_end=12.0)
    app._srt.close()

    assert submitted == [], f"captions mode submitted to the translator: {submitted}"

    body = app._srt.path.read_text(encoding="utf-8")
    assert body.startswith("1\n00:00:00,000 --> 00:00:03,000\n"), body[:60]
    assert "00:00:03,001 --> 00:00:12,000" in body, body
    assert "第一句中文字幕。" in body and "第二句中文字幕。" in body

    # translate mode must reach the translator (upstream behaviour preserved)
    app._pipeline_mode = modes.TRANSLATE
    app._process_segment_text("third", "en", 100.0, t_end=20.0)
    assert len(submitted) == 1, "translate mode did not submit a translation"

    return "captions never translates, SRT cues use the audio clock, translate still submits"


def main() -> int:
    print(f"project root: {ROOT}\n")
    check("2. import every module", _import_all)
    check("3. livetranslate.paths anchors", _paths)
    check("4. model-manager path aliases", _manager_paths)
    check("5. registry resolves + stays torch-free", _registry_lazy)
    check("6. ASR worker survives spawn", _worker_spawn)
    check("7. pipeline modes", _modes)
    check("8. cinema style presets", _presets)
    check("9. pipeline -> captions + SRT export", _pipeline_srt)

    print("PASSED:")
    for p in PASS:
        print(f"  [ok]   {p}")
    if FAIL:
        print("\nFAILED:")
        for f in FAIL:
            print(f"  [FAIL] {f}")
        return 1
    print("\nall runtime checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
