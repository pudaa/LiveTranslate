"""Caption pipeline modes -- how far an ASR result travels.

``captions`` (the "watch silently" mode)
    The ASR text **is** the subtitle. The translator is never called, so this
    mode needs no API key, no network and adds zero translation latency -- it
    works even with a bogus or unreachable translation endpoint. This is the
    mode for a video in a language you already read, which is the whole point of
    running LiveTranslate with a muted player in public.

``translate`` (upstream behaviour)
    Translate the ASR text into the target language. The overlay renders the
    original above the translation, so this doubles as a bilingual view; the
    standalone subtitle window renders whichever languages it is configured for.

Two notes on why this is a *pipeline* switch and not just a display toggle:

* Upstream had no switch at all. Translation was skipped only when the ASR
  language happened to equal the target language, so whether you paid for a
  translation call depended on language *detection* being right -- a short or
  noisy segment misdetected as a foreign language would fire a pointless
  translation request at a Chinese script.
* Keeping it out of the display layer means ``captions`` genuinely never
  touches the translator, which is what makes the zero-API-dependency promise
  hold rather than being an accident of the current UI state.
"""

from __future__ import annotations

CAPTIONS = "captions"
TRANSLATE = "translate"

MODES: tuple[str, ...] = (CAPTIONS, TRANSLATE)
DEFAULT: str = TRANSLATE


def normalize(mode: str | None) -> str:
    """Coerce an arbitrary setting to a known mode, defaulting silently."""
    return mode if mode in MODES else DEFAULT


def is_captions(mode: str | None) -> bool:
    return normalize(mode) == CAPTIONS
