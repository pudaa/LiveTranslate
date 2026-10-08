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
    "funasr": ("livetranslate.asr.backends.funasr", "build_engine"),
    "whisper": ("livetranslate.asr.backends.whisper", "build_engine"),
    "anime-whisper": ("livetranslate.asr.backends.anime_whisper", "build_engine"),
}

DEFAULT_BACKEND = "funasr"

# Upstream's ``_load_engine`` fell through to faster-whisper for any key it did
# not recognise. Kept verbatim so an unexpected value in a user's settings file
# degrades the same way it always has instead of hard-failing; it is logged at
# ERROR level so the mistake is still visible.
_FALLBACK_BACKEND = "whisper"


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
    """Instantiate an engine. Imports only the requested backend module."""
    module_name, factory_name = resolve(engine_type)
    log.info(f"Loading ASR backend: {engine_type!r} <- {module_name}.{factory_name}")
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
