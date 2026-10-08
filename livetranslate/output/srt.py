"""SRT caption writer.

Deliberately separate from :mod:`livetranslate.output.transcript`: that one
records wall-clock strings for a human reading a log, which is useless as a
subtitle file. An ``.srt`` needs a **start and an end** per cue, so the pipeline
has to hand this writer both.

Timing basis
------------
Cue times are seconds **relative to the start of the session**, taken from the
captured *audio* clock rather than the wall clock. Wall time would drift
whenever audio is dropped (a full ASR queue, a paused pipeline, a device
restart), which would slide the captions out of sync with the video they came
from -- and a subtitle file is only useful if it lines up.

Output is streamed: each cue is appended as soon as it is recognised, so the
file is already complete when playback ends and a crash mid-session still
leaves a usable prefix.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path

log = logging.getLogger("LiveTranslate.SRT")

# A cue shown for less than this reads as a flicker.
MIN_DURATION = 0.8
# Keep a gap between consecutive cues: some players treat end == next start as a
# hard cut and drop one of the two.
GAP = 0.001


def format_timestamp(seconds: float) -> str:
    """Format seconds as SRT's ``HH:MM:SS,mmm``."""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


class SRTWriter:
    """Append-only SRT writer, one file per session."""

    def __init__(self, base_dir: Path, enabled: bool = False):
        self._base_dir = Path(base_dir)
        self._enabled = bool(enabled)
        self._lock = threading.Lock()
        self._fp = None
        self._path: Path | None = None
        self._index = 0
        self._last_end = 0.0

    # ── public API ──────────────────────────────────────────────────────────

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def path(self) -> Path | None:
        return self._path

    def write(self, start: float, end: float, text: str, translation: str = "") -> None:
        """Append one cue. ``start``/``end`` are session-relative seconds."""
        text = (text or "").strip()
        translation = (translation or "").strip()
        if not text and not translation:
            return
        with self._lock:
            if not self._enabled:
                return
            if self._fp is None and not self._open_locked():
                return

            start, end = self._clamp(start, end)
            body = text if not translation else f"{text}\n{translation}"
            self._index += 1
            try:
                self._fp.write(
                    f"{self._index}\n"
                    f"{format_timestamp(start)} --> {format_timestamp(end)}\n"
                    f"{body}\n\n"
                )
            except OSError as exc:
                log.warning(f"SRT write failed: {exc}")
                return
            self._last_end = end

    def close(self) -> None:
        with self._lock:
            if self._fp is None:
                return
            try:
                self._fp.flush()
                self._fp.close()
            except Exception:
                pass
            self._fp = None
            log.info(f"SRT closed: {self._path} ({self._index} cues)")

    # ── internals ───────────────────────────────────────────────────────────

    def _clamp(self, start: float, end: float) -> tuple[float, float]:
        """Enforce minimum duration and forbid overlap with the previous cue."""
        try:
            start = float(start)
            end = float(end)
        except (TypeError, ValueError):
            start, end = self._last_end, self._last_end + MIN_DURATION
        if end < start:
            end = start
        if end - start < MIN_DURATION:
            end = start + MIN_DURATION
        # Never start before the previous cue finished.
        floor = self._last_end + GAP if self._index else 0.0
        if start < floor:
            start = floor
            end = max(end, start + MIN_DURATION)
        return start, end

    def _open_locked(self) -> bool:
        try:
            self._base_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error(f"Failed to create SRT dir {self._base_dir}: {exc}")
            self._enabled = False
            return False
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._path = self._base_dir / f"livetrans_{stamp}_captions.srt"
        try:
            # utf-8 (no BOM): VLC/mpv/PotPlayer all read it, and a BOM trips up
            # a few editors and subtitle tools. Line buffered so a tail or an
            # in-progress player sees cues as they land.
            self._fp = open(self._path, "w", encoding="utf-8", buffering=1)
        except OSError as exc:
            log.error(f"Failed to open SRT file {self._path}: {exc}")
            self._enabled = False
            return False
        log.info(f"SRT -> {self._path}")
        return True
