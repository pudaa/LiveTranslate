"""
Subtitle window - clean text-only window for OBS capture.
Uses QPainterPath for outlined text rendering.

Usage:
  - Middle-click drag to move the window
  - Configure via tray menu → Subtitle Mode → Settings
  - OBS: Window Capture → select "LiveTranslate Subtitle" → check "Allow Transparency"
"""

import ctypes
import math
import re
import time
from pathlib import Path

import json

from PyQt6.QtCore import (
    Qt, QPoint, QPointF, QRect, pyqtSignal, pyqtSlot, pyqtProperty,
    QPropertyAnimation, QParallelAnimationGroup, QEasingCurve, QTimer,
)
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    QBrush,
    QPixmap,
)
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout

from livetranslate.paths import APP_ROOT

# Win32 extended window style bits for OS-level mouse click-through (same
# mechanism as the main overlay): a window with WS_EX_TRANSPARENT passes clicks
# to whatever is behind it, so it never blocks the video player / browser.
_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x20


def _resolve_image_path(path: str) -> str:
    """Resolve image path (relative to project dir or absolute)."""
    if not path:
        return ""
    p = Path(path)
    if p.is_absolute():
        return str(p) if p.exists() else ""
    resolved = APP_ROOT / p
    return str(resolved) if resolved.exists() else ""

# Default subtitle window settings
DEFAULT_SUBTITLE_WIN_SETTINGS = {
    "enabled": False,
    "sentences": 1,
    "window_width": 860,
    "line_spacing": 8,
    "bg_color": "#000000",
    "bg_opacity": 76,
    "bg_image": "",
    "border_radius": 8,
    "auto_hide_timeout": 5,
    "auto_hide_animation": "fade",
    "auto_hide_duration": 300,
    # Subtitle sizing is relative: what matters is how many characters fit across
    # the video, and the video is often a small window rather than full screen.
    # One control that scales the bar and the type together keeps that ratio
    # adjustable instead of hard-coded.
    "size_scale": 1.0,
    # Pause width standing in for the silence that followed a sentence.
    "pause_gap_px_per_s": 70.0,
    "pause_gap_max_px": 170.0,
    # How fast the insertion offset settles, in ms. A time constant rather than a
    # speed, because the offset is always about one reading unit wide -- this way
    # each step is absorbed smoothly regardless of how large the unit is or how
    # quickly they arrive.
    "slide_settle_ms": 90,
    # Paced release. The recogniser hands over a whole phrase at once; releasing
    # it one reading unit at a time keeps every addition the same size and in the
    # same place, so the reader never has to re-scan the line to find the new
    # part. release_units_per_s is the floor; a long phrase drains over roughly
    # release_drain_s regardless, which is what keeps the caption real-time.
    "release_units_per_s": 8.0,
    "release_drain_s": 0.4,
    # How long a newly released unit takes to fade in -- the visual cue for
    # "this is what just appeared".
    "fresh_fade_in_ms": 220,
    # How long a corrected span keeps its dotted under-rule. A cue, not an alarm:
    # long enough to notice, short enough not to accumulate into clutter.
    "correction_mark_ms": 2500,
    # A just-corrected unit materialises from correction_fade_from to full over
    # correction_fade_in_ms. Starting partly visible, rather than from nothing, is
    # what keeps it distinguishable from genuinely new text.
    "correction_fade_in_ms": 320,
    "correction_fade_from": 0.45,
    # Largest mid-line hole tolerated while holding a corrected span's width, in
    # reading units. Small corrections -- the common case, one or two characters --
    # hold exactly; a large one would need a hole as wide as the text removed,
    # which is worse than the shift it avoids.
    "correction_pad_max_units": 2.2,
    # Minimum time a sentence stays up before it may be replaced, in ms.
    # Applied by delaying the replacement, so a large value makes live captions
    # lag the audio; 0 disables the throttle.
    "min_display_ms": 1500,
    "click_through": False,
    "lines": [
        {
            "type": "original",
            "enabled": True,
            "font_family": "Microsoft YaHei",
            "font_size": 20,
            "color": "#FFFFFF",
            "opacity": 255,
            "outline_enabled": True,
            "outline_color": "#000000",
            "outline_width": 2,
            "align": "center",
            "bg_image": "",
            "entry_animation": "none",
            "exit_animation": "none",
            "animation_duration": 300,
            # The original line scrolls; see _render_marquee_pixmap. The
            # translation line still wraps, because it is read as a block rather
            # than tracked word by word.
            "scroll": True,
            "fade_out_px": 96,
        },
        {
            "type": "translation",
            "lang": "zh",
            "enabled": True,
            "font_family": "Microsoft YaHei",
            "font_size": 23,
            "color": "#FFD700",
            "opacity": 255,
            "outline_enabled": True,
            "outline_color": "#000000",
            "outline_width": 2,
            "align": "center",
            "bg_image": "",
            "entry_animation": "none",
            "exit_animation": "none",
            "animation_duration": 300,
        },
    ],
}


def _merge_settings(base, override):
    result = {**base}
    for k, v in (override or {}).items():
        if k == "lines" and isinstance(v, list):
            result["lines"] = v
        else:
            result[k] = v
    return result


def _hex_to_rgba(hex_color: str, opacity: int) -> str:
    hex_color = hex_color.lstrip("#")
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return f"rgba({r},{g},{b},{opacity})"


_CJK_CHAR = re.compile(
    r"[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]"
)
_LATIN_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\u2019_\-]*")


def split_marquee_units(text: str) -> list:
    """Split text into the units the marquee advances and fades as blocks.

    The unit has to match how the language is read. A CJK character is a whole
    word, so one character per unit is right. Latin script is read per word:
    advancing half a word looks like a glitch, and a word that straddles the
    fade zone would half-dissolve. Drawing a whole word as one path also keeps
    its kerning and shaping intact, which per-character drawing would break.

    Trailing spaces ride along with the word so a line never starts with one.
    """
    units: list = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if _CJK_CHAR.match(ch):
            # Keep trailing CJK punctuation with its character, so a full stop
            # never fades out on its own.
            j = i + 1
            while j < n and text[j] in "\u3002\uff0c\u3001\uff1b\uff1a\uff01\uff1f\u201d\u300d\u300f":
                j += 1
            units.append(text[i:j])
            i = j
        elif _LATIN_WORD.match(ch):
            j = _LATIN_WORD.match(text, i).end()
            while j < n and text[j] in " \t":
                j += 1
            units.append(text[i:j])
            i = j
        else:
            units.append(ch)
            i += 1
    return units


class _SubtitleTextWidget(QWidget):
    """Renders outlined text using QPainterPath, with automatic word-wrap.
    Supports entry/exit animations via custom properties.
    """

    height_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self._wrapped_lines = []
        self._font = QFont("Microsoft YaHei", 24)
        self._color = QColor(255, 255, 255)
        self._outline_enabled = True
        self._outline_color = QColor(0, 0, 0)
        self._outline_width = 2
        self._align = "center"
        self._bg_pixmap = None
        self._text_cache = None
        self._last_width = 0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        # Animation state
        self._content_opacity_val = 1.0
        self._slide_offset_x_val = 0.0
        self._slide_offset_y_val = 0.0
        self._entry_animation = "none"
        self._exit_animation = "none"
        self._animation_duration = 300
        self._anim_group = None
        self._pending_text = None

        # Marquee mode: one non-wrapping line, anchored at the right edge, that
        # carries the words leftwards. Text leaves by fading out past the left
        # edge instead of by being replaced, so nothing the reader is looking at
        # ever jumps sideways. See set_marquee().
        self._scroll_mode = False
        self._marquee: list[list] = []  # [[text, gap_px_after], ...]
        # Insertion offset: newly arrived text is parked this far to the right and
        # animated back to zero, so it slides in instead of teleporting.
        self._marquee_slide_px = 0.0
        # Trailing units that were just released, and when. They fade in, which is
        # the cue for "this is the part that is new" -- without it the reader has
        # to re-scan the whole line to work out where the addition starts.
        self._fresh_units = 0
        self._fresh_at = 0.0
        self._corrections: list = []
        self._fresh_fade_in_s = 0.22
        self._correction_mark_s = 2.5
        self._correction_fade_in_s = 0.32
        self._correction_fade_from = 0.45
        self._fade_out_px = 96

    # --- pyqtProperty for content opacity ---
    def _get_content_opacity(self):
        return self._content_opacity_val

    def _set_content_opacity(self, val):
        self._content_opacity_val = val
        self.update()

    content_opacity = pyqtProperty(float, _get_content_opacity, _set_content_opacity)

    # --- pyqtProperty for slide offsets ---
    def _get_slide_offset_x(self):
        return self._slide_offset_x_val

    def _set_slide_offset_x(self, val):
        self._slide_offset_x_val = val
        self.update()

    slide_offset_x = pyqtProperty(float, _get_slide_offset_x, _set_slide_offset_x)

    def _get_slide_offset_y(self):
        return self._slide_offset_y_val

    def _set_slide_offset_y(self, val):
        self._slide_offset_y_val = val
        self.update()

    slide_offset_y = pyqtProperty(float, _get_slide_offset_y, _set_slide_offset_y)

    def set_config(self, cfg: dict, scale: float = 1.0):
        size = max(6, int(round(cfg.get("font_size", 24) * scale)))
        self._font = QFont(cfg.get("font_family", "Microsoft YaHei"), size)
        c = QColor(cfg.get("color", "#FFFFFF"))
        c.setAlpha(cfg.get("opacity", 255))
        self._color = c
        self._outline_enabled = cfg.get("outline_enabled", True)
        self._outline_color = QColor(cfg.get("outline_color", "#000000"))
        self._outline_width = cfg.get("outline_width", 2)
        self._align = cfg.get("align", "center")
        self._scroll_mode = bool(cfg.get("scroll", False))
        self._fade_out_px = int(cfg.get("fade_out_px", 96))
        self._fresh_fade_in_s = max(0.0, float(cfg.get("fresh_fade_in_ms", 220)) / 1000.0)
        self._correction_mark_s = max(0.0, float(cfg.get("correction_mark_ms", 2500)) / 1000.0)
        self._correction_fade_in_s = max(
            0.0, float(cfg.get("correction_fade_in_ms", 320)) / 1000.0
        )
        self._correction_fade_from = max(
            0.0, min(1.0, float(cfg.get("correction_fade_from", 0.45)))
        )
        resolved = _resolve_image_path(cfg.get("bg_image", ""))
        self._bg_pixmap = QPixmap(resolved) if resolved else None
        self._entry_animation = cfg.get("entry_animation", "none")
        self._exit_animation = cfg.get("exit_animation", "none")
        self._animation_duration = cfg.get("animation_duration", 300)
        self._text_cache = None
        self._update_height()
        self.update()

    def set_text(self, text: str):
        if self._text and text != self._text and self._exit_animation != "none":
            self._pending_text = text
            self._stop_all_animations()
            self.animate_out(callback=self._apply_pending_text)
            return

        self._apply_text_immediate(text)

    def _apply_pending_text(self):
        text = getattr(self, "_pending_text", "")
        self._pending_text = None
        self._apply_text_immediate(text)

    def _apply_text_immediate(self, text: str):
        # Stop any running animations and reset to final state
        self._stop_all_animations()
        self._content_opacity_val = 1.0
        self._slide_offset_x_val = 0.0
        self._slide_offset_y_val = 0.0
        self._pending_text = None

        self._text = text
        self._text_cache = None
        self._update_height()
        self.update()
        self.height_changed.emit()

        if text:
            self.animate_in()

    def _stop_all_animations(self):
        if self._anim_group and self._anim_group.state() != self._anim_group.State.Stopped:
            self._anim_group.stop()
        self._anim_group = None

    def animate_in(self):
        anim_type = self._entry_animation
        if anim_type == "none":
            self._content_opacity_val = 1.0
            self._slide_offset_x_val = 0.0
            self._slide_offset_y_val = 0.0
            self.update()
            return

        dur = self._animation_duration
        group = QParallelAnimationGroup(self)

        # Opacity animation (all types fade in)
        opacity_anim = QPropertyAnimation(self, b"content_opacity", self)
        opacity_anim.setDuration(dur)
        opacity_anim.setStartValue(0.0)
        opacity_anim.setEndValue(1.0)
        opacity_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        group.addAnimation(opacity_anim)

        w = self.width() or 200
        h = self.height() or 40

        if anim_type == "slide_left":
            slide = QPropertyAnimation(self, b"slide_offset_x", self)
            slide.setDuration(dur)
            slide.setStartValue(float(-w))
            slide.setEndValue(0.0)
            slide.setEasingCurve(QEasingCurve.Type.OutCubic)
            group.addAnimation(slide)
        elif anim_type == "slide_right":
            slide = QPropertyAnimation(self, b"slide_offset_x", self)
            slide.setDuration(dur)
            slide.setStartValue(float(w))
            slide.setEndValue(0.0)
            slide.setEasingCurve(QEasingCurve.Type.OutCubic)
            group.addAnimation(slide)
        elif anim_type == "slide_up":
            slide = QPropertyAnimation(self, b"slide_offset_y", self)
            slide.setDuration(dur)
            slide.setStartValue(float(h))
            slide.setEndValue(0.0)
            slide.setEasingCurve(QEasingCurve.Type.OutCubic)
            group.addAnimation(slide)
        elif anim_type == "slide_down":
            slide = QPropertyAnimation(self, b"slide_offset_y", self)
            slide.setDuration(dur)
            slide.setStartValue(float(-h))
            slide.setEndValue(0.0)
            slide.setEasingCurve(QEasingCurve.Type.OutCubic)
            group.addAnimation(slide)

        self._content_opacity_val = 0.0
        self.update()
        self._anim_group = group
        group.start()

    def animate_out(self, callback=None, anim_type=None, duration=None):
        if anim_type is None:
            anim_type = self._exit_animation
        if duration is None:
            duration = self._animation_duration
        if anim_type == "none":
            self._content_opacity_val = 0.0
            self.update()
            if callback:
                callback()
            return

        self._stop_all_animations()

        group = QParallelAnimationGroup(self)

        opacity_anim = QPropertyAnimation(self, b"content_opacity", self)
        opacity_anim.setDuration(duration)
        opacity_anim.setStartValue(self._content_opacity_val)
        opacity_anim.setEndValue(0.0)
        opacity_anim.setEasingCurve(QEasingCurve.Type.InCubic)
        group.addAnimation(opacity_anim)

        w = self.width() or 200
        h = self.height() or 40

        if anim_type == "slide_left":
            slide = QPropertyAnimation(self, b"slide_offset_x", self)
            slide.setDuration(duration)
            slide.setStartValue(0.0)
            slide.setEndValue(float(-w))
            slide.setEasingCurve(QEasingCurve.Type.InCubic)
            group.addAnimation(slide)
        elif anim_type == "slide_right":
            slide = QPropertyAnimation(self, b"slide_offset_x", self)
            slide.setDuration(duration)
            slide.setStartValue(0.0)
            slide.setEndValue(float(w))
            slide.setEasingCurve(QEasingCurve.Type.InCubic)
            group.addAnimation(slide)
        elif anim_type == "slide_up":
            slide = QPropertyAnimation(self, b"slide_offset_y", self)
            slide.setDuration(duration)
            slide.setStartValue(0.0)
            slide.setEndValue(float(-h))
            slide.setEasingCurve(QEasingCurve.Type.InCubic)
            group.addAnimation(slide)
        elif anim_type == "slide_down":
            slide = QPropertyAnimation(self, b"slide_offset_y", self)
            slide.setDuration(duration)
            slide.setStartValue(0.0)
            slide.setEndValue(float(h))
            slide.setEasingCurve(QEasingCurve.Type.InCubic)
            group.addAnimation(slide)

        if callback:
            group.finished.connect(callback)

        self._anim_group = group
        group.start()

    def split_text(self, text: str) -> list:
        """Split text into segments that fit within available width."""
        fm = QFontMetrics(self._font)
        ow = self._outline_width if self._outline_enabled else 0
        avail_w = self.width() - ow * 2
        if avail_w <= 0 or fm.horizontalAdvance(text) <= avail_w:
            return [text]

        segments = []
        while text:
            if fm.horizontalAdvance(text) <= avail_w:
                segments.append(text)
                break

            best = 0
            for i in range(1, len(text) + 1):
                if fm.horizontalAdvance(text[:i]) > avail_w:
                    break
                best = i
            if best == 0:
                best = 1

            # Prefer breaking at word/punctuation boundary
            break_at = best
            for j in range(best - 1, max(best // 2, 0), -1):
                if text[j] in ' ,，。、!！?？;；:：.':
                    break_at = j + 1
                    break

            segments.append(text[:break_at].rstrip())
            text = text[break_at:].lstrip()

        return segments or [text]

    def set_marquee(self, items: list, slide_px: float = 0.0,
                    fresh_units: int = 0, fresh_at: float = 0.0,
                    corrections: list = None) -> None:
        """Render a single, non-wrapping, right-anchored line.

        ``items`` is ``[[text, gap_px_after], ...]``. The gap after a segment is
        blank width standing in for the pause that followed it -- which is how a
        speaker's silence gets represented, rather than by punctuation the reader
        has no reason to understand.

        ``fresh_units`` trailing reading units fade in from ``fresh_at``, marking
        what has just been added.

        The newest text always ends at the right edge, so new words appear in a
        fixed place and the older words are what moves.
        """
        self._marquee = [[str(t), float(g or 0.0)] for t, g in items]
        self._marquee_slide_px = float(slide_px or 0.0)
        self._fresh_units = max(0, int(fresh_units))
        self._fresh_at = float(fresh_at or 0.0)
        # Entries are [units_from_end, count, age]; age drives the fade.
        self._corrections = [
            [int(e[0]), int(e[1]), float(e[2]) if len(e) > 2 else 0.0]
            for e in (corrections or [])
        ]
        # Reset the entry/exit animation state the way set_text() does. A value
        # left over from an unrelated animate_out() would otherwise keep the line
        # faded or offset, since this path does not go through set_text().
        self._stop_all_animations()
        self._content_opacity_val = 1.0
        self._slide_offset_x_val = 0.0
        self._slide_offset_y_val = 0.0
        self._text = "".join(t for t, _ in self._marquee)
        self._wrapped_lines = [self._text] if self._text else []
        self._pending_text = None
        self._text_cache = None
        self.setFixedHeight(self.desired_height())
        self.update()

    def marquee_width(self) -> float:
        """Rendered width of the whole marquee, gaps included."""
        if not self._marquee:
            return 0.0
        fm = QFontMetrics(self._font)
        total = 0.0
        for text, gap in self._marquee:
            for unit in split_marquee_units(str(text)):
                total += fm.horizontalAdvance(unit)
            total += gap
        return total

    def _rewrap(self):
        """Recalculate wrapped lines from current text."""
        if not self._text:
            self._wrapped_lines = []
        elif self._scroll_mode:
            # Never wrap: the line scrolls instead. Wrapping is what made the
            # display flip between one and two rows as sentences expired.
            self._wrapped_lines = [self._text]
        else:
            self._wrapped_lines = self.split_text(self._text)

    def desired_height(self) -> int:
        if not self._wrapped_lines:
            # An unused line must not reserve space. The translation line is
            # empty in captions / same-language mode, and giving it a full line
            # height left a blank gap under the line that does have content --
            # and inflated the window so it looked oversized.
            return 0
        fm = QFontMetrics(self._font)
        ow = self._outline_width if self._outline_enabled else 0
        return fm.lineSpacing() * len(self._wrapped_lines) + ow * 2 + 4

    def _update_height(self):
        self._rewrap()
        self.setFixedHeight(self.desired_height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        w = self.width()
        if w != self._last_width:
            self._last_width = w
            self._rewrap()
            self._text_cache = None
            self.setFixedHeight(self.desired_height())

    def _render_marquee_pixmap(self):
        w = self.width()
        h = self.desired_height()
        if w <= 0 or h <= 0 or not self._marquee:
            self._text_cache = None
            return

        dpr = self.devicePixelRatioF()
        pw, ph = int(w * dpr), int(h * dpr)
        if pw <= 0 or ph <= 0:
            self._text_cache = None
            return

        pix = QPixmap(pw, ph)
        pix.setDevicePixelRatio(dpr)
        pix.fill(QColor(0, 0, 0, 0))

        painter = QPainter(pix)
        if not painter.isActive():
            self._text_cache = None
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        fm = QFontMetrics(self._font)
        ow = self._outline_width if self._outline_enabled else 0
        # Snap to device pixels. The glyphs are laid out at fractional x while
        # the line scrolls, and an unaligned glyph is re-sampled by the rasteriser
        # -- which is what makes scrolling text look soft compared with static
        # text. Snapping to 1/dpr keeps it as sharp as it can be at any scale.
        dpr = self.devicePixelRatioF() or 1.0

        def snap(value: float) -> float:
            return round(value * dpr) / dpr

        baseline = snap(ow + fm.ascent())

        outline_pen = None
        if self._outline_enabled and self._outline_width > 0:
            outline_pen = QPen(
                self._outline_color,
                self._outline_width * 2,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )

        # Right-anchored: the end of the line is pinned to the right edge, so the
        # newest words land in a fixed place and the older ones travel left.
        x = float(w - ow - self.marquee_width() + self._marquee_slide_px)
        all_units = [u for text, _ in self._marquee for u in split_marquee_units(str(text))]
        fresh_from = max(0, len(all_units) - self._fresh_units)
        corrected = set()
        corrected_fade = {}
        for entry in self._corrections:
            # Entries are [units_from_end, count, age] -- unpack explicitly.
            start = len(all_units) - int(entry[0])
            age = float(entry[2]) if len(entry) > 2 else 0.0
            fade = self._correction_fade(age)
            for index in range(start, start + int(entry[1])):
                corrected.add(index)
                corrected_fade[index] = max(corrected_fade.get(index, 0.0), fade)
        spans = []
        ui = 0
        for text, gap in self._marquee:
            for unit in split_marquee_units(str(text)):
                fade_in = 1.0
                if ui >= fresh_from and self._fresh_units and self._fresh_at:
                    elapsed = time.monotonic() - self._fresh_at
                    fade_in = min(1.0, elapsed / max(0.01, self._fresh_fade_in_s))
                fade_in = min(fade_in, corrected_fade.get(ui, 1.0))
                ux = snap(x)
                uw = fm.horizontalAdvance(unit)
                self._draw_marquee_unit(
                    painter, fm, unit, ux, baseline, outline_pen, fade_in
                )
                if ui in corrected and unit.strip():
                    if spans and abs(spans[-1][1] - ux) < 1.0:
                        spans[-1][1] = ux + uw  # merge with the previous unit
                    else:
                        spans.append([ux, ux + uw])
                x += uw
                ui += 1
            x += gap
        self._draw_correction_marks(painter, spans, baseline, fm)

        painter.end()
        self._text_cache = pix

    def _draw_marquee_unit(self, painter, fm, unit, x, baseline, outline_pen, fade_in=1.0):
        """Draw one reading unit, fading it in when new and out as it leaves.

        The unit -- a CJK character, or a whole Latin word -- is drawn as a
        single path. Per character would break Latin kerning and would let a
        word dissolve half at a time, which reads as a rendering fault rather
        than as text leaving.
        """
        if not unit.strip():
            return
        cw = fm.horizontalAdvance(unit)
        if x + cw <= -self._fade_out_px:
            return  # fully past the fade zone
        if x >= 0:
            alpha = 1.0
        else:
            alpha = max(0.0, 1.0 + x / max(1, self._fade_out_px))
        alpha *= max(0.0, min(1.0, fade_in))
        if alpha <= 0.0:
            return

        path = QPainterPath()
        path.addText(x, baseline, self._font, unit)

        if outline_pen is not None:
            painter.setPen(outline_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

        colour = QColor(self._color)
        colour.setAlphaF(colour.alphaF() * alpha)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(colour))
        painter.drawPath(path)

    def _correction_fade(self, age: float) -> float:
        """Visibility of a just-corrected unit, as a gentle materialisation.

        Deliberately weaker than the fade-in new text gets: starting from a
        partly visible value reads as "this changed", whereas starting from
        nothing reads as "this is new" -- the cue the reader must not confuse it
        with. It settles at full strength after correction_fade_in_ms.
        """
        fade = max(0.0, self._correction_fade_in_s)
        if fade <= 0:
            return 1.0
        floor = max(0.0, min(1.0, self._correction_fade_from))
        return floor + (1.0 - floor) * min(1.0, max(0.0, age / fade))

    def _draw_correction_marks(self, painter, spans, baseline, fm):
        """Rule a fine dotted line under the spans the recogniser corrected.

        A dotted rule rather than a flash. A flash reads as "this is new", which is
        the cue the fade-in already carries, so the reader re-reads the whole span
        before finding that only a word or two changed -- worse than no cue at all.
        A rule underneath is legible as an annotation without competing with the
        text, and it fades out on its own.
        """
        if not spans or not self._corrections or self._correction_mark_s <= 0:
            return
        ttl = self._correction_mark_s
        alpha = 0.0
        for entry in self._corrections:
            age = entry[2] if len(entry) > 2 else 0.0
            # Fade over the last 0.6s rather than vanishing outright.
            alpha = max(alpha, min(1.0, max(0.0, (ttl - age) / 0.6)))
        if alpha <= 0.0:
            return

        colour = QColor(self._color)
        colour.setAlphaF(colour.alphaF() * 0.85 * alpha)
        pen = QPen(colour)
        pen.setWidthF(max(1.0, self._outline_width * 0.9))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setDashPattern([1.0, 2.0])  # fine dots, not a solid rule
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        y = min(self.height() - 1.0, baseline + max(2.0, fm.descent() * 0.6))
        for x0, x1 in spans:
            painter.drawLine(QPointF(x0, y), QPointF(x1, y))

    def _render_text_pixmap(self):
        if self._scroll_mode:
            self._render_marquee_pixmap()
            return
        lines = self._wrapped_lines or [self._text]
        w = self.width()
        h = self.desired_height()
        if w <= 0 or h <= 0:
            self._text_cache = None
            return

        dpr = self.devicePixelRatioF()
        pw, ph = int(w * dpr), int(h * dpr)
        if pw <= 0 or ph <= 0:
            self._text_cache = None
            return

        pix = QPixmap(pw, ph)
        pix.setDevicePixelRatio(dpr)
        pix.fill(QColor(0, 0, 0, 0))

        painter = QPainter(pix)
        if not painter.isActive():
            self._text_cache = None
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        fm = QFontMetrics(self._font)
        ow = self._outline_width if self._outline_enabled else 0
        y = ow + fm.ascent()

        path = QPainterPath()
        for line in lines:
            text_w = fm.horizontalAdvance(line)
            if self._align == "center":
                lx = (w - text_w) / 2
            elif self._align == "right":
                lx = w - text_w - ow
            else:
                lx = ow
            path.addText(lx, y, self._font, line)
            y += fm.lineSpacing()

        if self._outline_enabled and self._outline_width > 0:
            pen = QPen(self._outline_color, self._outline_width * 2,
                       Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(self._color))
        painter.drawPath(path)
        painter.end()

        self._text_cache = pix

    def paintEvent(self, event):
        if not self._text:
            return

        if self._text_cache is None:
            self._render_text_pixmap()
        if self._text_cache is None:
            return

        painter = QPainter(self)
        painter.setOpacity(self._content_opacity_val)

        if self._bg_pixmap and not self._bg_pixmap.isNull():
            painter.drawPixmap(self.rect(), self._bg_pixmap)

        painter.drawPixmap(
            int(self._slide_offset_x_val),
            int(self._slide_offset_y_val),
            self._text_cache,
        )

        painter.end()


class SubtitleWindow(QWidget):
    """Clean text-only subtitle window for OBS capture.

    Middle-click anywhere to drag. Window width is fixed (set in settings),
    height auto-fits to text content.
    """

    update_text_signal = pyqtSignal(str, str)  # original, translations_json
    partial_text_signal = pyqtSignal(str)
    position_changed = pyqtSignal()
    window_closed = pyqtSignal()

    def __init__(self, settings=None):
        super().__init__()
        self._settings = _merge_settings(DEFAULT_SUBTITLE_WIN_SETTINGS, settings)
        self._text_widgets = []
        self._sentences = []  # [(original, {lang: text, ...}), ...]
        # The sentence currently being spoken. Live captioning has to show it
        # growing as it is recognised; waiting for the sentence to finish means
        # the subtitle arrives after the audio it describes, which is the one
        # thing a live caption must not do.
        self._partial = ""
        # Pause width after each sentence, in px, parallel to _sentences. A
        # speaker's silence is drawn as blank width rather than as a separator
        # character, so the reader sees time pass instead of punctuation.
        self._gaps: list[float] = []
        self._live_gap_px = 0.0
        # When the most recent text arrived, and the pause width fixed at the
        # moment speech resumed (None while the speaker is still silent).
        self._last_text_at = 0.0
        self._frozen_gap = None
        # Insertion offset for newly arrived text, eased to zero by the animation
        # timer, plus the last measured text width so a growth can be detected.
        self._slide_px = 0.0
        self._last_text_width = 0.0
        self._last_tick = 0.0
        # Paced release. The recogniser hands over a whole phrase at once, which
        # lands as one lump the reader has to re-scan to find the new part, and
        # the lump size varies. Text is buffered here and released one reading
        # unit at a time instead: every addition is then the same size and
        # appears in the same place, with no re-scanning.
        self._release_queue = ""   # units recognised but not on screen yet
        self._released_tail = ""   # recognised text actually on screen
        # What is *rendered* for that tail. Kept separate from _released_tail
        # because a correction pads the replacement to preserve the line's width,
        # and padding must not pollute the watermark the prefix check runs on.
        self._display_tail = ""
        self._release_credit = 0.0
        self._fresh_count = 0
        self._fresh_at = 0.0
        # Corrections currently being marked: [units from the end, unit count,
        # when applied]. Counted from the end because text is only ever appended
        # there or trimmed from the front, so the offset stays valid.
        self._corrections: list[list] = []
        # Correction in flight. The replaced span is removed at once, but its width
        # is held by padding that is consumed as the replacement types in, so the
        # line neither moves nor changes width while that happens.
        self._corr_active = False
        self._corr_prefix = ""
        self._corr_old = ""
        self._corr_new = ""
        self._corr_shown = ""
        self._marquee_timer = QTimer(self)
        self._marquee_timer.setInterval(16)  # ~60fps
        self._marquee_timer.timeout.connect(self._tick_marquee)
        self._drag_pos = None
        self._bg_pixmap = None
        self._click_through = bool(self._settings.get("click_through", False))
        # Qt clears the extended style on show/raise/flag changes, so re-assert it
        # on a timer rather than only once.
        self._ct_timer = QTimer(self)
        self._ct_timer.setInterval(500)
        self._ct_timer.timeout.connect(self._apply_click_through)
        # Auto-hide state
        self._auto_hide_timer = QTimer(self)
        self._auto_hide_timer.setSingleShot(True)
        self._auto_hide_timer.timeout.connect(self._on_auto_hide_timeout)
        self._is_hidden_by_timeout = False
        # Pending overflow segments for delayed insertion
        self._pending_segment_timers = []
        # Minimum display time: queue rapid updates instead of replacing instantly
        self._last_insert_time = 0.0
        # Minimum time a sentence stays on screen before it may be replaced.
        #
        # A minimum duration is standard subtitle practice, but applying it by
        # *delaying the replacement* means the display falls behind the audio:
        # while the previous line is held, later speech has already been
        # recognised, and when the swap finally happens the new line is longer
        # than the utterance it describes. For live captions a short value is
        # usually better; 0 removes the throttle entirely.
        self._min_display_ms = int(self._settings.get("min_display_ms", 1500))
        self._height_anim = None

        self._setup_ui()
        self.update_text_signal.connect(self._on_update_text)
        self.partial_text_signal.connect(self._on_partial_text)

    @staticmethod
    def _is_pos_visible(x, y, margin=50):
        for screen in QApplication.screens():
            geo = screen.availableGeometry()
            if geo.left() <= x + margin and x < geo.right() and geo.top() <= y + margin and y < geo.bottom():
                return True
        return False

    def _clamp_to_screen(self):
        x, y = self.x(), self.y()
        if self._is_pos_visible(x, y):
            return
        screen = QApplication.screenAt(QPoint(x, y))
        if screen is None:
            screen = QApplication.primaryScreen()
        geo = screen.availableGeometry()
        nx = max(geo.left(), min(x, geo.right() - self.width()))
        ny = max(geo.top(), min(y, geo.bottom() - self.height()))
        self.move(nx, ny)

    def _setup_ui(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setWindowTitle("LiveTranslate Subtitle")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        s = self._settings
        saved_x = s.get("window_x")
        saved_y = s.get("window_y")
        if saved_x is not None and saved_y is not None:
            if self._is_pos_visible(saved_x, saved_y):
                self.move(saved_x, saved_y)
            else:
                self.move(100, 100)
        else:
            self.move(100, 100)
        self.setFixedWidth(self._effective_width())

        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(0, 0, 0, 0)
        self._main_layout.setSpacing(0)

        # Content area
        self._content = QWidget()
        self._content.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(16, 8, 16, 8)
        self._content_layout.setSpacing(s.get("line_spacing", 8))

        self._rebuild_text_widgets()

        self._main_layout.addWidget(self._content)

        self._apply_background()
        self._fit_height_animated()
        self.set_click_through(self._click_through)

    def _size_scale(self) -> float:
        """Overall caption scale, clamped to a sane range."""
        try:
            return max(0.4, min(2.0, float(self._settings.get("size_scale", 1.0))))
        except (TypeError, ValueError):
            return 1.0

    def _effective_width(self) -> int:
        return int(self._settings.get("window_width", 860) * self._size_scale())

    def _rebuild_text_widgets(self):
        for w in self._text_widgets:
            self._content_layout.removeWidget(w)
            w.deleteLater()
        self._text_widgets = []

        for line_cfg in self._settings.get("lines", []):
            if not line_cfg.get("enabled", True):
                continue
            tw = _SubtitleTextWidget()
            tw.set_config(line_cfg, self._size_scale())
            tw.height_changed.connect(self._fit_height_animated)
            self._text_widgets.append(tw)
            self._content_layout.addWidget(tw)

    def _apply_background(self):
        s = self._settings
        resolved = _resolve_image_path(s.get("bg_image", ""))
        if resolved:
            self._bg_pixmap = QPixmap(resolved)
            self._content.setStyleSheet("background: transparent;")
        else:
            self._bg_pixmap = None
            color = s.get("bg_color", "#000000")
            opacity = s.get("bg_opacity", 0)
            if opacity == 0:
                self._content.setStyleSheet("background: transparent;")
            else:
                rgba = _hex_to_rgba(color, opacity)
                radius = s.get("border_radius", 8)
                self._content.setStyleSheet(f"background: {rgba}; border-radius: {radius}px;")
        self.update()

    def _calc_target_height(self):
        margins = self._content_layout.contentsMargins()
        spacing = self._content_layout.spacing()
        # Empty lines contribute nothing, including no separator spacing.
        heights = [h for h in (tw.desired_height() for tw in self._text_widgets) if h > 0]
        total = margins.top() + margins.bottom() + sum(heights)
        if len(heights) > 1:
            total += spacing * (len(heights) - 1)
        return max(total, 20)

    def _fit_height_snap(self):
        new_h = self._calc_target_height()
        old_h = self.height()
        if new_h == old_h:
            return
        if self._height_anim and self._height_anim.state() != QPropertyAnimation.State.Stopped:
            self._height_anim.stop()
        # Anchor the bottom edge, not the centre. A caption bar sits on the video's
        # lower edge, so growing text should extend upward and leave the baseline
        # where the user put it. Centring moved the window on every height change,
        # so a position the user set never held.
        self.move(self.x(), self.y() + old_h - new_h)
        self.setFixedHeight(new_h)
        self._clamp_to_screen()
        self.position_changed.emit()

    def _fit_height_animated(self):
        new_h = self._calc_target_height()
        old_h = self.height()
        if new_h == old_h:
            return
        if self._height_anim and self._height_anim.state() != QPropertyAnimation.State.Stopped:
            self._height_anim.stop()

        # Bottom-anchored, same as the snap variant: keep y + height constant.
        target_y = self.y() + old_h - new_h
        self.setMinimumHeight(min(old_h, new_h))
        self.setMaximumHeight(max(old_h, new_h))

        anim = QPropertyAnimation(self, b"geometry")
        anim.setDuration(150)
        anim.setStartValue(QRect(self.x(), self.y(), self.width(), old_h))
        anim.setEndValue(QRect(self.x(), target_y, self.width(), new_h))
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        def on_finished():
            self.setFixedHeight(new_h)
            self._clamp_to_screen()
            self.position_changed.emit()
        anim.finished.connect(on_finished)

        self._height_anim = anim
        anim.start()

    def apply_settings(self, settings: dict):
        self._settings = _merge_settings(DEFAULT_SUBTITLE_WIN_SETTINGS, settings)

        for w in self._text_widgets:
            self._content_layout.removeWidget(w)
            w.deleteLater()
        self._text_widgets = []

        for line_cfg in self._settings.get("lines", []):
            if not line_cfg.get("enabled", True):
                continue
            tw = _SubtitleTextWidget()
            tw.set_config(line_cfg, self._size_scale())
            tw.height_changed.connect(self._fit_height_animated)
            self._text_widgets.append(tw)
            self._content_layout.addWidget(tw)

        self._content_layout.setSpacing(self._settings.get("line_spacing", 8))

        self.setFixedWidth(self._effective_width())

        self._apply_background()
        self._refresh_display()
        self.set_click_through(self._settings.get("click_through", False))

        # Reset auto-hide timer with new settings
        self._restart_auto_hide_timer()

    # --- Auto-hide ---
    def _restart_auto_hide_timer(self):
        timeout = self._settings.get("auto_hide_timeout", 0)
        self._auto_hide_timer.stop()
        if timeout > 0 and self._sentences:
            self._auto_hide_timer.setInterval(timeout * 1000)
            self._auto_hide_timer.start()

    def _on_auto_hide_timeout(self):
        if self._is_hidden_by_timeout:
            return
        self._is_hidden_by_timeout = True
        anim_type = self._settings.get("auto_hide_animation", "fade")
        duration = self._settings.get("auto_hide_duration", 300)
        for tw in self._text_widgets:
            tw.animate_out(anim_type=anim_type, duration=duration)

    def _restore_from_auto_hide(self):
        if not self._is_hidden_by_timeout:
            return
        self._is_hidden_by_timeout = False
        anim_type = self._settings.get("auto_hide_animation", "fade")
        duration = self._settings.get("auto_hide_duration", 300)
        # Reverse the hide animation type for restore
        restore_type = anim_type
        if anim_type == "slide_down":
            restore_type = "slide_up"
        elif anim_type == "slide_up":
            restore_type = "slide_down"
        elif anim_type == "slide_left":
            restore_type = "slide_right"
        elif anim_type == "slide_right":
            restore_type = "slide_left"

        for tw in self._text_widgets:
            tw._stop_all_animations()
            # Set hidden state
            tw._content_opacity_val = 0.0
            if restore_type == "slide_left":
                tw._slide_offset_x_val = float(-(tw.width() or 200))
            elif restore_type == "slide_right":
                tw._slide_offset_x_val = float(tw.width() or 200)
            elif restore_type == "slide_up":
                tw._slide_offset_y_val = float(tw.height() or 40)
            elif restore_type == "slide_down":
                tw._slide_offset_y_val = float(-(tw.height() or 40))
            tw.update()
            # Use the entry animation mechanism with overridden type
            old_entry = tw._entry_animation
            old_dur = tw._animation_duration
            tw._entry_animation = restore_type if restore_type != "none" else "fade"
            tw._animation_duration = duration
            tw.animate_in()
            tw._entry_animation = old_entry
            tw._animation_duration = old_dur

    # --- Click-through ---
    def set_click_through(self, enabled: bool):
        self._click_through = bool(enabled)
        if self._click_through:
            if not self._ct_timer.isActive():
                self._ct_timer.start()
        else:
            self._ct_timer.stop()
        self._apply_click_through()

    def _apply_click_through(self):
        try:
            hwnd = int(self.winId())
            style = ctypes.windll.user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
            if self._click_through:
                if not (style & _WS_EX_TRANSPARENT):
                    ctypes.windll.user32.SetWindowLongW(
                        hwnd, _GWL_EXSTYLE, style | _WS_EX_TRANSPARENT
                    )
            elif style & _WS_EX_TRANSPARENT:
                ctypes.windll.user32.SetWindowLongW(
                    hwnd, _GWL_EXSTYLE, style & ~_WS_EX_TRANSPARENT
                )
        except Exception:
            pass

    def showEvent(self, event):
        super().showEvent(event)
        # Qt resets the extended style across hide/show, so re-assert it here.
        self._apply_click_through()

    # --- Middle-click drag ---
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self._drag_pos = (
                event.globalPosition().toPoint()
                - self.frameGeometry().topLeft()
            )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() & Qt.MouseButton.MiddleButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            if self._drag_pos:
                self._drag_pos = None
                self.position_changed.emit()
        super().mouseReleaseEvent(event)

    def closeEvent(self, event):
        self.window_closed.emit()
        super().closeEvent(event)

    def paintEvent(self, event):
        if self._bg_pixmap and not self._bg_pixmap.isNull():
            painter = QPainter(self)
            painter.drawPixmap(self.rect(), self._bg_pixmap)
            painter.end()
        super().paintEvent(event)

    # --- Text updates ---
    def update_text(self, original: str, translations: dict | str):
        """Thread-safe text update.

        translations: dict mapping lang code to translated text,
                      or a plain string (backward compat, treated as primary target).
        """
        if isinstance(translations, str):
            # Backward compat: wrap in dict with empty key
            translations = {"": translations}
        self.update_text_signal.emit(original, json.dumps(translations, ensure_ascii=False))

    @pyqtSlot(str, str)
    def update_partial(self, text: str):
        """Replace the in-progress sentence.

        Called repeatedly while the speaker is still talking, so it *replaces*
        rather than appends -- the text is expected to grow on each call.
        """
        self.partial_text_signal.emit(text)

    @pyqtSlot(str)
    def _on_partial_text(self, text: str):
        text = text or ""
        appeared = bool(text) and not self._partial
        self._partial = text
        if appeared:
            # Speech resumed. Fix the pause that preceded it here, or the gap
            # would go on widening while the words are already on screen.
            self._mark_text_arrived()
        self._queue_partial(text)
        if text:
            self._restart_auto_hide_timer()
        # Content changed, so re-render through the normal path: that is also
        # where the insertion offset gets compensated.
        self._refresh_display()
        self._start_marquee()

    def _on_update_text(self, original: str, translations_json: str):
        translations = json.loads(translations_json)
        self._cancel_pending_segments()

        # Respect minimum display time: delay if previous sentence was inserted recently
        now_ms = time.monotonic() * 1000
        elapsed = now_ms - self._last_insert_time
        if self._scroll_line_config() is not None:
            # A marquee has no such thing as a minimum display time: the text is
            # already on screen as the in-progress line, so deferring the
            # replacement only makes the committed line show up late.
            base_delay = 0
        elif self._last_insert_time > 0:
            base_delay = max(0, int(self._min_display_ms - elapsed))
        else:
            base_delay = 0

        if base_delay == 0:
            self._insert_sentence(original, translations)
        else:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(base_delay)
            timer.timeout.connect(lambda o=original, t=translations: self._insert_sentence(o, t))
            timer.start()
            self._pending_segment_timers.append(timer)

    def _insert_sentence(self, original: str, translations: dict):
        """Insert a single sentence and refresh display."""
        # Put up anything still buffered first, so the committed line is complete
        # and _display_tail holds the whole recognised text.
        self._flush_release()

        # Carry the padded display text into the sentence. The padding exists to
        # hold the line's width steady through a correction; dropping it here would
        # shift everything again, at the moment the reader is most likely to still
        # be looking at the corrected words. The transcript is unaffected --
        # main.py writes the true text before the window ever sees it.
        display = self._display_tail if self._display_tail != self._released_tail else original

        max_sentences = self._settings.get("sentences", 1)
        # Mutable entries: the marquee trims leading characters in place.
        self._sentences.append([display, translations])
        if self._scroll_line_config() is None:
            # Wrapped mode keeps count-based expiry. Scroll mode decays by width
            # instead, so nothing jumps when a sentence ages out.
            if len(self._sentences) > max_sentences:
                self._sentences = self._sentences[-max_sentences:]
        else:
            # The pause that just ended belongs to the sentence before this one.
            silence = self._pause_gap_px()
            if self._gaps:
                self._gaps[-1] = silence
            while len(self._gaps) < len(self._sentences):
                self._gaps.append(0.0)

        self._live_gap_px = 0.0
        self._frozen_gap = None
        self._last_text_at = time.monotonic()
        self._released_tail = ""
        self._display_tail = ""
        self._release_queue = ""
        self._release_credit = 0.0
        self._finish_correction()
        # The committed text now covers whatever the partial line was showing.
        self._partial = ""
        self._start_marquee()

        if self._is_hidden_by_timeout:
            self._restore_from_auto_hide()

        self._refresh_display()
        self._restart_auto_hide_timer()
        self._last_insert_time = time.monotonic() * 1000

    def _cancel_pending_segments(self):
        """Cancel any pending delayed segment insertions."""
        for timer in self._pending_segment_timers:
            timer.stop()
            timer.deleteLater()
        self._pending_segment_timers.clear()


    def _scroll_line_config(self):
        """The first enabled line configured to scroll, or None."""
        for cfg in self._settings.get("lines", []):
            if cfg.get("enabled", True) and cfg.get("scroll"):
                return cfg
        return None

    def _pad_replacement(self, old_text: str, new_text: str, shown_text: str,
                         limit_px: float = None) -> str:
        """Padding that holds a corrected span's width while it fills in.

        A correction must not change the line's total width: the line is
        right-anchored, so any difference shifts everything to the left of it.

        The width to hold is ``pad + shown``, which stays constant if the padding
        covers both the part of the replacement not yet typed *and* the residual
        left over once it is complete::

            pad = (width(new) - width(shown)) + final_residual

        Starting from the whole replacement width and shrinking as it types in is
        what keeps the line still for the whole operation, not just at the end.

        The residual is capped by ``limit_px``: holding it exactly through a large
        replacement would need a hole as wide as the text removed, and a big hole
        mid-line is worse than the shift it avoids. The uncapped remainder is left
        to the slide animation instead.

        Padding goes before the replacement, not after. Trailing padding sits at
        the line's right edge, where it reads as the caption failing to keep up; a
        gap inside the line reads as a pause, which is what a gap already means.
        """
        tw = self._text_widgets[0] if self._text_widgets else None
        if tw is None or not old_text:
            return ""
        fm = QFontMetrics(tw._font)
        new_w = fm.horizontalAdvance(new_text or "")
        lost = fm.horizontalAdvance(old_text) - new_w
        residual = max(0.0, lost)
        if limit_px is not None:
            residual = min(residual, limit_px)
        remaining = max(0.0, new_w - fm.horizontalAdvance(shown_text or ""))
        total = residual + remaining
        if total <= 0:
            return ""
        pad_char = "\u3000" if fm.horizontalAdvance("\u3000") > 0 else " "
        pad_w = fm.horizontalAdvance(pad_char) or 1
        return pad_char * int(total // pad_w)

    def _pad_limit_px(self) -> float:
        """Largest hole tolerated mid-line, in px."""
        tw = self._text_widgets[0] if self._text_widgets else None
        if tw is None:
            return 0.0
        fm = QFontMetrics(tw._font)
        units = float(self._settings.get("correction_pad_max_units", 2.2))
        return fm.horizontalAdvance("\u3000") * max(0.0, units)

    def _rebuild_display_tail(self) -> None:
        """Recompose the rendered tail while a correction is filling in."""
        if not self._corr_active:
            return
        self._display_tail = self._corr_prefix + self._pad_replacement(
            self._corr_old, self._corr_new, self._corr_shown, self._pad_limit_px()
        ) + self._corr_shown

    def _start_correction(self, prefix: str, old_suffix: str, new_suffix: str) -> None:
        """Replace a span by typing the replacement in behind reserved space.

        The replaced text is removed at once -- leaving known-wrong words up while
        the replacement types in would be worse -- but its width is held by
        padding that is consumed as the replacement arrives. The result is that the
        line neither moves nor changes width during the whole operation.
        """
        self._corr_active = True
        self._corr_prefix = prefix
        self._corr_old = old_suffix
        self._corr_new = new_suffix
        self._corr_shown = ""
        self._release_queue = new_suffix
        self._release_credit = 0.0
        self._released_tail = prefix
        self._rebuild_display_tail()

        # Whatever the cap did not hold shows up as movement. Hand it to the slide
        # so it eases over the settle constant rather than jumping.
        tw = self._text_widgets[0] if self._text_widgets else None
        if tw is not None and old_suffix:
            fm = QFontMetrics(tw._font)
            held = fm.horizontalAdvance(self._pad_replacement(
                old_suffix, new_suffix, "", self._pad_limit_px()
            )) + fm.horizontalAdvance(new_suffix or "")
            lost = fm.horizontalAdvance(old_suffix) - held
            if lost > 0:
                self._slide_px -= lost

    def _finish_correction(self) -> None:
        self._corr_active = False
        self._corr_prefix = ""
        self._corr_old = ""
        self._corr_new = ""
        self._corr_shown = ""

    def _mark_correction(self, units_from_end: int, count: int) -> None:
        """Record a corrected span so it can be underlined for a while."""
        if count <= 0 or units_from_end <= 0:
            return
        self._corrections = [
            c for c in self._corrections if not (c[0] == units_from_end and c[1] == count)
        ]
        self._corrections.append([units_from_end, count, time.monotonic()])

    def _live_corrections(self) -> list:
        """Corrections still inside their marking window, as (from_end, count, age)."""
        ttl = max(0.0, float(self._settings.get("correction_mark_ms", 2500)) / 1000.0)
        if ttl <= 0:
            self._corrections.clear()
            return []
        now = time.monotonic()
        self._corrections = [c for c in self._corrections if now - c[2] < ttl]
        return [(c[0], c[1], now - c[2]) for c in self._corrections]

    def _queue_partial(self, text: str) -> None:
        """Buffer the part of the in-progress text that is not on screen yet.

        Partials are *replacements*, not increments ("那么" then "那么第三"), so
        the increment has to be derived against what has already been shown.
        """
        text = text or ""
        if text.startswith(self._released_tail):
            self._release_queue = text[len(self._released_tail):]
            return

        # The recogniser revised what it had rather than extending it (SenseVoice
        # corrects earlier guesses: 它是 -> 他是). Replace the in-progress text in
        # place. Re-queueing the whole sentence, as this used to, showed the words
        # already on screen a second time.
        keep = 0
        common = min(len(text), len(self._released_tail))
        while keep < common and text[keep] == self._released_tail[keep]:
            keep += 1

        changed_old = self._released_tail[keep:]
        changed_new = text[keep:]
        # Route the replacement through the same buffered, one-unit-at-a-time
        # release that new text gets. Replacing the span outright made it appear as
        # one lump -- exactly the problem the release queue exists to solve.
        self._start_correction(text[:keep], changed_old, changed_new)

        from_end = len(split_marquee_units(changed_new))
        self._mark_correction(from_end, len(split_marquee_units(changed_new)))

    def _release_step(self, dt: float) -> bool:
        """Show at most a little more of the buffered text. True if anything moved.

        The rate is a floor plus a catch-up term, so an ordinary partial is
        released as a smooth trickle and a long one still drains promptly. That
        is what keeps the caption real-time while the increments stay even.
        """
        if not self._release_queue:
            return False
        units = split_marquee_units(self._release_queue)
        if not units:
            self._release_queue = ""
            return False

        base = float(self._settings.get("release_units_per_s", 8.0))
        drain = max(0.15, float(self._settings.get("release_drain_s", 0.4)))
        rate = max(base, len(units) / drain)
        self._release_credit += rate * dt

        released = 0
        while self._release_credit >= 1.0 and self._release_queue:
            head = split_marquee_units(self._release_queue)[0]
            self._released_tail += head
            if self._corr_active:
                # While a correction fills in, the release feeds the replacement so
                # the padding can shrink by exactly what has arrived.
                self._corr_shown += head
                self._rebuild_display_tail()
            else:
                self._display_tail += head
            self._release_queue = self._release_queue[len(head):]
            self._release_credit -= 1.0
            released += 1
        if released and self._corr_active and not self._release_queue:
            self._finish_correction()
            self._rebuild_display_tail()
        if released:
            self._fresh_count = released
            self._fresh_at = time.monotonic()
            # A released unit appears where it belongs and fades in; only a real
            # jump should be parked off the right edge. See _compensate_insertion.
            self._compensate_insertion()
        return bool(released)

    def _flush_release(self) -> None:
        """Show everything buffered at once (used when a sentence commits)."""
        if not self._release_queue:
            return
        self._released_tail += self._release_queue
        if self._corr_active:
            self._corr_shown += self._release_queue
            self._finish_correction()
            self._rebuild_display_tail()
        else:
            self._display_tail += self._release_queue
        self._fresh_count = len(split_marquee_units(self._release_queue))
        self._fresh_at = time.monotonic()
        self._release_queue = ""
        self._release_credit = 0.0

    def _pause_gap_px(self) -> float:
        """Blank width for the silence currently on display.

        Frozen once text is arriving: a gap that keeps widening after the next
        sentence is already on screen reads as the speaker both talking and
        pausing at the same time.
        """
        rate = float(self._settings.get("pause_gap_px_per_s", 70.0))
        cap = float(self._settings.get("pause_gap_max_px", 170.0))
        if self._partial and self._frozen_gap is not None:
            return self._frozen_gap
        if self._last_text_at <= 0:
            return 0.0
        return min(cap, (time.monotonic() - self._last_text_at) * rate)

    def _mark_text_arrived(self) -> None:
        """New text landed: the pause is over, so fix its width and restart the
        clock for the pause that comes after."""
        if self._frozen_gap is None and self._last_text_at > 0:
            self._frozen_gap = self._pause_gap_px()
        self._last_text_at = time.monotonic()

    def _tick_marquee(self) -> None:
        """One animation frame.

        The line's position is animated rather than derived from the content.
        Deriving it means every arriving chunk of text teleports the line by that
        chunk's width, which is what made the motion read as a stutter.
        """
        now = time.monotonic()
        dt = max(0.0, min(0.1, now - self._last_tick))
        self._last_tick = now

        # Release one reading unit at a time from whatever has been buffered.
        moved = self._release_step(dt)

        if self._slide_px > 0.0:
            # Exponential settle rather than a fixed speed: the offset is always
            # about one reading unit wide, and a time constant absorbs each step
            # smoothly no matter how big it is or how fast they arrive.
            tau = max(0.02, float(self._settings.get("slide_settle_ms", 90)) / 1000.0)
            self._slide_px *= math.exp(-dt / tau)
            if self._slide_px < 0.5:
                self._slide_px = 0.0
            moved = True

        gap = self._pause_gap_px()
        if abs(gap - self._live_gap_px) >= 0.5:
            self._live_gap_px = gap
            moved = True

        # Keep ticking while a fade-in or a correction mark is still playing:
        # stopping here would leave them frozen part-way.
        fade = max(0.0, float(self._settings.get("fresh_fade_in_ms", 220)) / 1000.0)
        if self._fresh_count and self._fresh_at and (now - self._fresh_at) < fade:
            moved = True
        mark_ttl = max(0.0, float(self._settings.get("correction_mark_ms", 2500)) / 1000.0)
        if any(now - c[2] < mark_ttl for c in self._corrections):
            moved = True

        if not moved:
            self._marquee_timer.stop()
            return
        self._render_marquee()

    def _start_marquee(self) -> None:
        if self._scroll_line_config() is None:
            return
        self._last_tick = time.monotonic()
        if not self._marquee_timer.isActive():
            self._marquee_timer.start()

    def _enabled_lines(self) -> list:
        return [ln for ln in self._settings.get("lines", []) if ln.get("enabled", True)]

    def _render_marquee(self) -> None:
        """Repaint the scrolling lines only; this runs on every animation frame."""
        for wi, cfg in enumerate(self._enabled_lines()):
            if wi >= len(self._text_widgets):
                break
            tw = self._text_widgets[wi]
            if not (cfg.get("scroll") and tw._scroll_mode):
                continue
            if cfg.get("type", "original") == "original":
                # Every release adds width, and without this the whole line would
                # jump left by that width on each frame's release.
                self._compensate_insertion()
            tw.set_marquee(
                self._marquee_items(cfg.get("type", "original"), cfg),
                self._slide_px,
                self._fresh_count,
                self._fresh_at,
                self._live_corrections(),
            )

    def _typical_unit_px(self) -> float:
        """Width of one reading unit at the current font, roughly."""
        tw = self._text_widgets[0] if self._text_widgets else None
        if tw is None:
            return 0.0
        return float(QFontMetrics(tw._font).horizontalAdvance("中") or 16)

    def _compensate_insertion(self) -> None:
        """Park a large jump of new text off to the right for the animation to carry in.

        A jump is only compensated when it is markedly bigger than one reading
        unit. With paced release a unit lands several times a second, and parking
        each one off the right edge reads as the caption perpetually sliding
        itself back in -- which is not the cue we want. A single unit simply
        appears where it belongs and fades in, which is what marks it as new.

        Only *text* growth is considered at all: the pause gap is meant to push
        the line along, since that is what shows time passing.
        """
        width = self._marquee_text_width()
        if self._last_text_width:
            growth = width - self._last_text_width
            if abs(growth) > self._typical_unit_px() * 1.6:
                self._slide_px += growth
        self._last_text_width = width

    def _marquee_text_width(self) -> float:
        """Width of the marquee's text, excluding the pause gaps."""
        tw = self._text_widgets[0] if self._text_widgets else None
        if tw is None:
            return 0.0
        fm = QFontMetrics(tw._font)
        total = 0.0
        for original in (s[0] for s in self._sentences):
            for unit in split_marquee_units(original):
                total += fm.horizontalAdvance(unit)
        for unit in split_marquee_units(self._display_tail):
            total += fm.horizontalAdvance(unit)
        return total

    def _marquee_items(self, line_type: str, cfg: dict) -> list:
        """Build ``[[text, gap_px_after], ...]`` for one scroll-mode line."""
        items: list[list] = []
        for i, (original, tl_dict) in enumerate(self._sentences):
            if line_type == "original":
                text = original
            else:
                text = self._translation_for(tl_dict, cfg)
            if not text:
                continue
            gap = self._gaps[i] if i < len(self._gaps) else 0.0
            if i == len(self._sentences) - 1:
                # The silence on display right now: it widens until speech
                # resumes, then stops.
                gap = self._pause_gap_px()
            items.append([text, gap])
        if self._display_tail:
            items.append([self._display_tail, 0.0])
        return items

    @staticmethod
    def _translation_for(tl_dict, cfg: dict) -> str:
        if isinstance(tl_dict, str):
            return tl_dict
        lang = cfg.get("lang", "")
        if lang and lang in tl_dict:
            return tl_dict[lang]
        if "" in tl_dict and tl_dict[""]:
            return tl_dict[""]
        for value in tl_dict.values():
            if value:
                return value
        return ""

    def _trim_marquee(self, tw) -> None:
        """Drop what has travelled past the left edge.

        Trimming is by rendered width, not by sentence count. Expiring whole
        sentences is what made the display jump between one and two rows, and it
        yanks the text the reader is following sideways; trimming a few
        characters at a time lets the line leave the way it arrived.
        """
        if tw is None or not self._sentences or tw.width() <= 0:
            return
        cfg = self._scroll_line_config() or {}
        limit = tw.width() + int(cfg.get("fade_out_px", 96))
        fm = QFontMetrics(tw._font)
        for _ in range(200):  # bounded: each pass drops at least one character
            total = sum(fm.horizontalAdvance(s[0]) for s in self._sentences)
            total += sum(self._gaps) + fm.horizontalAdvance(self._display_tail)
            if total <= limit or not self._sentences:
                return
            head = self._sentences[0][0]
            if not head:
                self._sentences.pop(0)
                if self._gaps:
                    self._gaps.pop(0)
                continue
            excess = total - limit
            all_text = "".join(s[0] for s in self._sentences)
            avg = max(1.0, total / max(1, len(all_text)))
            drop = max(1, min(len(head), int(excess / avg) or 1))
            self._sentences[0][0] = head[drop:]

    def _refresh_display(self):
        # The in-progress line counts as content: at the start of an utterance
        # there are no committed sentences yet, and that is exactly when the
        # caption most needs to be on screen.
        if not self._sentences and not self._display_tail and not self._release_queue:
            for tw in self._text_widgets:
                tw.set_text("")
            self._fit_height_snap()
            return

        lines_cfg = [ln for ln in self._settings.get("lines", []) if ln.get("enabled", True)]
        wi = 0

        for cfg in lines_cfg:
            if wi >= len(self._text_widgets):
                break
            tw = self._text_widgets[wi]
            line_type = cfg.get("type", "original")

            if cfg.get("scroll") and tw._scroll_mode:
                items = self._marquee_items(line_type, cfg)
                if line_type == "original":
                    self._trim_marquee(tw)
                    items = self._marquee_items(line_type, cfg)
                    self._compensate_insertion()
                tw.set_marquee(
                    items, self._slide_px, self._fresh_count, self._fresh_at,
                    self._live_corrections(),
                )
                wi += 1
                continue

            if line_type == "original":
                texts = [s[0] for s in self._sentences if s[0]]
                if self._partial:
                    # The in-progress sentence rides at the end of the original
                    # line, so the line grows instead of a new one appearing.
                    texts.append(self._partial)
            else:
                lang = cfg.get("lang", "")
                texts = []
                for _, tl_dict in self._sentences:
                    if isinstance(tl_dict, str):
                        if tl_dict:
                            texts.append(tl_dict)
                    elif lang and lang in tl_dict:
                        texts.append(tl_dict[lang])
                    elif "" in tl_dict and tl_dict[""]:
                        texts.append(tl_dict[""])
                    else:
                        for v in tl_dict.values():
                            if v:
                                texts.append(v)
                                break
            # A wide space, not "|": a pipe between sentences is notation the
            # reader has no reason to interpret.
            tw.set_text("　".join(texts) if len(texts) > 1 else (texts[0] if texts else ""))
            wi += 1

    def get_target_languages(self) -> set:
        """Return set of unique target language codes from enabled translation lines."""
        langs = set()
        for cfg in self._settings.get("lines", []):
            if cfg.get("enabled", True) and cfg.get("type") == "translation":
                lang = cfg.get("lang", "")
                if lang:
                    langs.add(lang)
        return langs

    def clear(self):
        self._sentences.clear()
        self._gaps.clear()
        self._partial = ""
        self._released_tail = ""
        self._display_tail = ""
        self._release_queue = ""
        self._release_credit = 0.0
        self._fresh_count = 0
        self._fresh_at = 0.0
        self._corrections.clear()
        self._finish_correction()
        self._live_gap_px = 0.0
        self._frozen_gap = None
        self._last_text_at = 0.0
        self._slide_px = 0.0
        self._last_text_width = 0.0
        self._marquee_timer.stop()
        self._cancel_pending_segments()
        self._auto_hide_timer.stop()
        self._is_hidden_by_timeout = False
        for tw in self._text_widgets:
            tw._stop_all_animations()
            tw._content_opacity_val = 1.0
            tw._slide_offset_x_val = 0.0
            tw._slide_offset_y_val = 0.0
            tw.set_text("")
        self._fit_height_snap()
