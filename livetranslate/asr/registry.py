"""ASR backend registry.

Resolution is deliberately **lazy**: the backend module is imported the first
time its key is requested. Importing every backend eagerly would drag torch /
faster-whisper / funasr into whichever process touched the registry -- and the
GUI process must never load them (see ``CLAUDE.md``: the ASR engine is only ever
instantiated inside the worker subprocess).

Adding a backend:

1. drop a module into ``livetranslate/asr/backends/`` exposing
   ``build_engine(config: dict) -> engine`` -- the factory owns *all*
   backend-specific argument handling (device parsing, compute type, paths)
2. add one line to ``_MANIFEST`` below

Engine contract (duck-typed; nothing enforces it at runtime)::

    transcribe(audio: np.ndarray) -> {"text", "language", ...} | None   # required
    set_language(language: str)          # optional
    set_input_padding(seconds: float)    # optional
    to_device(device: str)               # optional
    unload()                             # optional, called on worker shutdown
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

log = logging.getLogger("LiveTranslate.ASR.Registry")

# engine key -> (module, factory)
_MANIFEST: dict[str, tuple[str, str]] = {
    "sherpa-onnx": ("livetranslate.asr.backends.sherpa_onnx", "build_engine"),
    "funasr": ("livetranslate.asr.backends.funasr", "build_engine"),
    "whisper": ("livetranslate.asr.backends.whisper", "build_engine"),
    "anime-whisper": ("livetranslate.asr.backends.anime_whisper", "build_engine"),
}

# sherpa-onnx is the default because it is the only backend with **no torch
# dependency**: it runs SenseVoice-Small as int8 ONNX on the CPU (~215ms for an
# 8s segment against a 600ms budget) and leaves the dGPU in P8 (~2.7W) instead
# of P0 (~21W / 39% util). That also makes it the only backend that survives a
# CPU-only release build. The torch-based engines stay in-repo for anyone who
# wants GPU inference -- see _BACKEND_DEPS for how they are gated.
DEFAULT_BACKEND = "sherpa-onnx"

# Runtime dependencies per engine. Used to (a) hide engines a CPU-only build
# cannot run, and (b) degrade gracefully instead of hard-failing when a settings
# file names an engine this build has no runtime for -- an existing user's
# user_settings.json still says "funasr" after the default changed.
_BACKEND_DEPS: dict[str, tuple[str, ...]] = {
    "sherpa-onnx": ("sherpa_onnx",),
    "funasr": ("torch", "funasr"),
    "whisper": ("torch", "faster_whisper"),
    "anime-whisper": ("torch", "transformers"),
}

# Any unrecognised key degrades to the default (logged at ERROR level) rather
# than hard-failing. Upstream fell through to faster-whisper here; that is no
# longer a safe target, because a CPU-only build has no faster-whisper at all.
_FALLBACK_BACKEND = DEFAULT_BACKEND


def _has_module(name: str) -> bool:
    """Whether a module is importable, without importing it."""
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def backend_available(engine_type: str | None) -> bool:
    """True when this build can actually load the engine."""
    deps = _BACKEND_DEPS.get(engine_type or "")
    if deps is None:
        return True  # unknown engine: let resolve()/build() report it
    return all(_has_module(dep) for dep in deps)


def available_backends() -> tuple[str, ...]:
    """Engine keys this build can load -- what the UI should offer."""
    return tuple(key for key in _MANIFEST if backend_available(key))


def known_backends() -> tuple[str, ...]:
    """Engine keys this build can load."""
    return tuple(_MANIFEST)


def resolve(engine_type: str | None) -> tuple[str, str]:
    """Return ``(module, factory)`` for an engine key."""
    key = engine_type or DEFAULT_BACKEND
    spec = _MANIFEST.get(key)
    if spec is None:
        log.error(
            f"Unknown ASR engine_type={key!r}; falling back to "
            f"{_FALLBACK_BACKEND!r}. Known engines: {', '.join(known_backends())}"
        )
        spec = _MANIFEST[_FALLBACK_BACKEND]
    return spec


def build(engine_type: str | None, config: dict) -> Any:
    """Instantiate an engine. Imports only the requested backend module.

    Degrades to ``DEFAULT_BACKEND`` when the requested engine's runtime is not
    installed -- e.g. a CPU-only build asked for ``"funasr"`` because the user's
    settings file predates the default change. Without this the user would be
    left with no ASR at all and only a traceback in the log.
    """
    key = engine_type or DEFAULT_BACKEND
    if not backend_available(key):
        missing = ", ".join(_BACKEND_DEPS.get(key, ())) or "unknown"
        log.error(
            f"ASR engine {key!r} is not usable in this build (missing: "
            f"{missing}); falling back to {DEFAULT_BACKEND!r}"
        )
        key = DEFAULT_BACKEND

    module_name, factory_name = resolve(key)
    log.info(f"Loading ASR backend: {key!r} <- {module_name}.{factory_name}")
    module = importlib.import_module(module_name)
    factory = getattr(module, factory_name)
    return factory(config)


def parse_device(device: str) -> tuple[str, int]:
    """Split ``"cuda:0 (RTX 4090)"`` into ``("cuda", 0)``.

    Whisper (ctranslate2) only accepts ``device="cuda"`` -- never ``"cuda:0"`` --
    so the index travels separately via its ``device_index`` argument.
    """
    device = str(device or "cpu").split(" (", 1)[0].strip()
    if device.startswith("cuda:"):
        index = int(device.split(":", 1)[1])
        return "cuda", index
    return device, 0
