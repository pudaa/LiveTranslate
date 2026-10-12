"""SenseVoice-Small on ONNX Runtime, via sherpa-onnx -- the default backend.

Why this backend exists
-----------------------
The PyTorch/funasr path pins the NVIDIA GPU awake: measured on the dev machine
an 8s segment costs ~95ms but leaves the dGPU in **P0 at ~21W / 39% util**,
against **P8 / 2.7W** when only the CPU is used. It also drags ~2.5GB of CUDA
runtime into a one-file build. sherpa-onnx runs *the same SenseVoiceSmall
weights* as int8 ONNX on the CPU in ~215ms per 8s segment -- 2.5x inside the
600ms `interim_interval` budget -- with a ~19MB runtime and no torch at all.

This module deliberately imports **no torch**. That is what makes a CPU-only
release possible (see build_exe.py / build_release.ps1), so please keep it that
way.

Two behaviours were established by measurement (2026-10-11) and are handled
here rather than in the caller:

1. **sherpa's SenseVoice decode emits no punctuation.** The tokens are in the
   vocab (``。`` at line 9730 of tokens.txt, identical 25055-entry vocab as
   funasr's tokens.json) but greedy int8 decoding drops them:
   ``去陈述教育所以教育的陈述方式...`` instead of ``去陈述教育。所以教育的陈述方式...``.
   ``use_itn`` makes no difference, and beam search is refused outright
   (``Only greedy_search is supported at present``). So a CT-Transformer
   punctuation model (75MB) runs on the *text*, costing ~15ms.
2. **Only ``greedy_search`` is supported** -- there is no decoding_method to
   tune. Do not add one; it raises at load time.

Result mapping matches the other backends: ``{text, language, language_name}``,
with the language tag parsed out of SenseVoice's ``<|zh|>`` / ``<|yue|>`` style
markers exactly as ``backends/sensevoice.py`` does.
"""

from __future__ import annotations

import logging
import re

import numpy as np

from livetranslate.models.manager import (
    DEFAULT_SHERPA_MODEL,
    get_sherpa_model_paths,
    sherpa_profile,
)

log = logging.getLogger("LiveTranslate.SherpaOnnx")

SAMPLE_RATE = 16000
DEFAULT_THREADS = 4
DEFAULT_PUNCT_THREADS = 1

# SenseVoice emits language / emotion / event as <|xx|> markers.
_TAG_RE = re.compile(r"<\|[^|]*\|>")

# sherpa reports the language as a marker string ('<|zh|>'); the pipeline wants
# the bare code. Unknown markers fall through as-is so a new language is still
# visible in the UI rather than silently becoming "auto".
_LANG_MAP = {
    "<|zh|>": "zh",
    "<|en|>": "en",
    "<|ja|>": "ja",
    "<|ko|>": "ko",
    "<|yue|>": "yue",
}

# Measured 2026-10-11: on 5/5 eight-second clips of a Mandarin lecture sherpa
# reported <|yue|> where funasr reported <|zh|> for byte-identical audio, so the
# yue token is not trustworthy on this model + runtime. The difference is not
# cosmetic downstream: yasbd has no 'yue' segmenter and silently falls back to
# 'en' (see LiveTranslateApp._get_segmenter), which would sentence-split a
# Mandarin lecture with a Latin splitter. Mandarin and Cantonese are both CJK
# and share the same punctuation, so normalising to 'zh' is safe -- and the raw
# marker is still logged below, so the real detection is never lost.
_LANG_NORMALISE = {"yue": "zh"}


class SherpaOnnxEngine:
    """Speech-to-text using sherpa-onnx (ONNX Runtime, CPU only).

    CPU-only by design: ``to_device`` is implemented as a no-op rather than
    being omitted, so the pipeline's optional-device path finds a method and
    logs the request instead of raising AttributeError.
    """

    def __init__(
        self,
        model_key: str | None = None,
        threads: int = DEFAULT_THREADS,
        use_punctuation: bool = True,
    ):
        self._model_key = model_key or DEFAULT_SHERPA_MODEL
        self._threads = max(1, int(threads))
        self._use_punctuation = bool(use_punctuation)
        self._recognizer = None
        self._punctuation = None
        self._paths = None
        self.language = None  # None = let the model detect it

        self._require_model()
        self._build()
        log.info(
            "sherpa-onnx loaded: %s on cpu "
            "(threads=%d, punctuation=%s)"
            % (
                sherpa_profile(self._model_key)["display_name"],
                self._threads,
                "on" if self._punctuation is not None else "off",
            )
        )

    # ── construction ───────────────────────────────────────────────────────

    def _require_model(self):
        self._paths = get_sherpa_model_paths(
            self._model_key, punctuation=self._use_punctuation
        )
        if self._paths is None:
            profile = sherpa_profile(self._model_key)
            from livetranslate.models.manager import sherpa_model_dir

            raise FileNotFoundError(
                f"sherpa-onnx model '{profile['display_name']}' is missing or "
                f"incomplete under {sherpa_model_dir(self._model_key)}. "
                f"Expected {profile['model_file']}"
                + (f" and {profile['tokens_file']}" if profile.get("tokens_file") else "")
                + "."
            )

    def _build(self):
        import sherpa_onnx

        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(self._paths["model"]),
            tokens=str(self._paths["tokens"]),
            num_threads=self._threads,
            use_itn=True,
            # Empty string = auto-detect. SenseVoice predicts the language
            # itself and reports it in result.lang, so pinning it here would
            # only fight the model.
            language="",
            debug=False,
        )

        punct_path = self._paths.get("punctuation")
        if punct_path is None:
            if self._use_punctuation:
                # Not fatal -- the subtitles still work, they just lose their
                # commas and full stops, which measurably hurts readability.
                log.warning(
                    "sherpa-onnx punctuation model not found; subtitles will "
                    "have no punctuation. Run tools/prefetch_models.py or see "
                    "models/manager.py SHERPA_MODEL_PROFILES."
                )
            return

        config = sherpa_onnx.OfflinePunctuationConfig(
            model=sherpa_onnx.OfflinePunctuationModelConfig(
                ct_transformer=str(punct_path),
                num_threads=DEFAULT_PUNCT_THREADS,
                provider="cpu",
                debug=False,
            )
        )
        self._punctuation = sherpa_onnx.OfflinePunctuation(config)

    # ── engine contract ────────────────────────────────────────────────────

    def set_language(self, language: str):
        """Record the requested language.

        sherpa takes the language at recogniser-construction time and does not
        expose it afterwards, so switching rebuilds the recogniser (~2s). The
        value is only used as a *hint*; SenseVoice detects the language itself,
        which is why ``auto`` maps to "leave it alone".
        """
        wanted = None if language in (None, "", "auto") else language
        if wanted == self.language:
            return
        old = self.language
        self.language = wanted
        log.info(f"sherpa-onnx language: {old} -> {wanted} (auto-detected)")
        # No rebuild: an empty ``language`` (auto) is what the recogniser was
        # built with, and SenseVoice predicts the language regardless. A
        # rebuild here would cost ~2s per settings save for no behaviour change.

    def set_input_padding(self, pad_seconds):
        """No-op: sherpa does no padding bucketing, so there is nothing to set.

        The funasr backend pads to a 0.5s bucket to keep batch shapes stable;
        sherpa decodes one segment at a time and has no equivalent knob.
        """
        return

    def to_device(self, device: str):
        """CPU-only backend; report that the request was ignored."""
        if str(device).lower().startswith("cuda"):
            log.info(
                "sherpa-onnx is a CPU-only backend; ignoring device=%r. "
                "Select a torch-based engine to use the GPU." % device
            )
        return False

    def unload(self):
        self._recognizer = None
        self._punctuation = None
        self._paths = None

    def transcribe(self, audio: np.ndarray) -> dict | None:
        """Transcribe 16kHz mono float32 audio."""
        if self._recognizer is None:
            raise RuntimeError("sherpa-onnx engine was unloaded")

        samples = np.ascontiguousarray(audio, dtype=np.float32)
        if samples.size == 0:
            return None

        stream = self._recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        self._recognizer.decode_stream(stream)
        result = stream.result

        raw_text = (getattr(result, "text", "") or "").strip()
        if not raw_text:
            return None

        detected = self._parse_language(getattr(result, "lang", ""))
        text = self._punctuate(raw_text)

        log.debug(f"Raw: {raw_text}")
        return {
            "text": text,
            "language": detected,
            "language_name": detected,
        }

    # ── helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_language(marker: str | None) -> str:
        if not marker:
            return "auto"
        raw = str(marker).strip()
        # Unmapped marker: strip the <| |> decoration so it stays readable.
        code = _LANG_MAP.get(raw) or _TAG_RE.sub("", raw).strip()
        if not code:
            return "auto"
        normalised = _LANG_NORMALISE.get(code, code)
        if normalised != code:
            log.debug(f"language {code!r} normalised to {normalised!r} (raw={raw!r})")
        return normalised

    def _punctuate(self, text: str) -> str:
        if self._punctuation is None:
            return text
        try:
            restored = self._punctuation.add_punctuation(text)
        except Exception:
            # Punctuation is an enhancement; never lose the transcript over it.
            log.warning("punctuation model failed; returning raw text", exc_info=True)
            return text
        return (restored or text).strip()


def build_engine(config: dict) -> SherpaOnnxEngine:
    """Factory for livetranslate.asr.registry."""
    return SherpaOnnxEngine(
        model_key=config.get("sherpa_model"),
        threads=config.get("sherpa_threads", DEFAULT_THREADS),
        use_punctuation=config.get("sherpa_punctuation", True),
    )
