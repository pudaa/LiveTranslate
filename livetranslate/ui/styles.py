"""Caption style presets.

There are **two** independent schemas, because the two windows are different
things:

* ``SubtitleOverlay`` (``overlay.py``) is a *chat transcript*: every line
  carries ``[timestamp] [language] text  ASR 320ms`` metadata. Its style dict
  keys are ``bg_color`` / ``bg_opacity`` / ``original_color`` / ... -- see
  ``DEFAULT_STYLE`` there. It renders rich text (QLabel), so it **cannot draw
  text outlines**; "cinema" for the overlay therefore means *low visual noise*
  plus an optional metadata-free "pure" rendering.
* ``SubtitleWindow`` (``subtitle_window.py``) is a *caption bar* rendered with
  per-line ``QPainterPath`` outlines -- this is the surface that can reproduce a
  native player's look (translucent black plate, or a pure outline over the
  video). Its settings use a ``lines`` list; see
  ``DEFAULT_SUBTITLE_WIN_SETTINGS`` there.

Both groups live here so the two stay legible side by side and the control panel
has one place to enumerate them.
"""

from __future__ import annotations

from livetranslate.ui.subtitle_window import DEFAULT_SUBTITLE_WIN_SETTINGS

# ── Overlay presets ──────────────────────────────────────────────────────────
# Keys must match SubtitleOverlay.DEFAULT_STYLE. Opacity fields are 0-255
# (bg/header) or 0-100 (window); sizes are points.
#
# ``show_meta`` is an extension: when False the overlay drops the
# timestamp/language/ASR-ms chrome and shows the text alone, which is what makes
# it read like native subtitles instead of a debug console.

CINEMA_OVERLAY_PRESETS: dict[str, dict] = {
    "cinema_black": {
        "preset": "cinema_black",
        "bg_color": "#000000",
        "bg_opacity": 205,
        "header_color": "#000000",
        "header_opacity": 180,
        "border_radius": 6,
        "original_font_size": 12,
        "translation_font_size": 15,
        "original_color": "#e6e6e6",
        "translation_color": "#ffffff",
        "timestamp_color": "#8a8a8a",
        "window_opacity": 100,
        "show_meta": False,
    },
    "cinema_yellow": {
        "preset": "cinema_yellow",
        "bg_color": "#000000",
        "bg_opacity": 190,
        "header_color": "#000000",
        "header_opacity": 170,
        "border_radius": 6,
        "original_font_size": 12,
        "translation_font_size": 15,
        "original_color": "#d8d8d8",
        "translation_color": "#ffeb3b",
        "timestamp_color": "#8a8a8a",
        "window_opacity": 100,
        "show_meta": False,
    },
    "cinema_soft": {
        "preset": "cinema_soft",
        "bg_color": "#14161a",
        "bg_opacity": 215,
        "header_color": "#14161a",
        "header_opacity": 200,
        "border_radius": 10,
        "original_font_size": 12,
        "translation_font_size": 15,
        "original_color": "#cfd3d8",
        "translation_color": "#f5f2ea",
        "timestamp_color": "#7d818a",
        "window_opacity": 100,
        "show_meta": False,
    },
    "cinema_solid": {
        "preset": "cinema_solid",
        "bg_color": "#000000",
        "bg_opacity": 255,
        "header_color": "#000000",
        "header_opacity": 255,
        "border_radius": 0,
        "original_font_size": 12,
        "translation_font_size": 16,
        "original_color": "#ffffff",
        "translation_color": "#ffffff",
        "timestamp_color": "#9a9a9a",
        "window_opacity": 100,
        "show_meta": False,
    },
}

# Which key a "reset to plain" action should restore for the overlay.
CINEMA_OVERLAY_DEFAULT = "cinema_black"


# ── Subtitle-window presets ──────────────────────────────────────────────────
# Built on top of the shipped line templates so every field the settings dialog
# reads (font_family, opacity, animations, ...) is present.

_SUBWIN_ORIGINAL = DEFAULT_SUBTITLE_WIN_SETTINGS["lines"][0]
_SUBWIN_TRANSLATION = DEFAULT_SUBTITLE_WIN_SETTINGS["lines"][1]


def _line(template: dict, **overrides) -> dict:
    return {**template, **overrides}


def _preset(
    *,
    bg_opacity: int,
    border_radius: int,
    line_spacing: int,
    text_color: str,
    text_size: int,
    original_size: int,
    outline: bool,
    outline_width: int,
    outline_color: str = "#000000",
    text_opacity: int = 255,
) -> dict:
    return {
        "bg_color": "#000000",
        "bg_opacity": bg_opacity,
        "border_radius": border_radius,
        "line_spacing": line_spacing,
        "lines": [
            _line(
                _SUBWIN_ORIGINAL,
                font_size=original_size,
                color=text_color,
                opacity=text_opacity,
                outline_enabled=outline,
                outline_color=outline_color,
                outline_width=outline_width,
            ),
            _line(
                _SUBWIN_TRANSLATION,
                font_size=text_size,
                color=text_color,
                opacity=text_opacity,
                outline_enabled=outline,
                outline_color=outline_color,
                outline_width=outline_width,
            ),
        ],
    }


CINEMA_SUBWIN_PRESETS: dict[str, dict] = {
    # The genuine native-player look: no plate at all, legibility comes from the
    # outline drawn over the video.
    "cinema_outline": _preset(
        bg_opacity=0,
        border_radius=0,
        line_spacing=6,
        text_color="#ffffff",
        text_size=30,
        original_size=24,
        outline=True,
        outline_width=3,
    ),
    # Netflix-ish: translucent black plate with rounded corners, no outline.
    "cinema_plate": _preset(
        bg_opacity=110,
        border_radius=10,
        line_spacing=8,
        text_color="#ffffff",
        text_size=28,
        original_size=22,
        outline=False,
        outline_width=0,
    ),
    # Broadcast/yellow: plate + yellow text, thin outline for edge definition.
    "cinema_yellow": _preset(
        bg_opacity=130,
        border_radius=6,
        line_spacing=8,
        text_color="#ffe14d",
        text_size=28,
        original_size=22,
        outline=True,
        outline_width=2,
    ),
    # Big, heavy outline for small windows or a far-from-screen view.
    "cinema_bold": _preset(
        bg_opacity=0,
        border_radius=0,
        line_spacing=10,
        text_color="#ffffff",
        text_size=34,
        original_size=26,
        outline=True,
        outline_width=4,
    ),
}

CINEMA_SUBWIN_DEFAULT = "cinema_outline"


# ── Shared enumeration for the UI ────────────────────────────────────────────

# (key, i18n label key) -- cinema first so it is the obvious choice.
CINEMA_OVERLAY_ITEMS: list[tuple[str, str]] = [
    ("cinema_black", "preset_cinema_black"),
    ("cinema_yellow", "preset_cinema_yellow"),
    ("cinema_soft", "preset_cinema_soft"),
    ("cinema_solid", "preset_cinema_solid"),
]

CINEMA_SUBWIN_ITEMS: list[tuple[str, str]] = [
    ("cinema_outline", "preset_cinema_outline"),
    ("cinema_plate", "preset_cinema_plate"),
    ("cinema_yellow", "preset_cinema_subwin_yellow"),
    ("cinema_bold", "preset_cinema_bold"),
]
