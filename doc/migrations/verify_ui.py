"""Headless UI smoke test for the P3 caption-style work.

The static checker cannot see a Qt typo, and the style preset path touches three
widgets (control panel -> overlay style dict -> ChatMessage rendering; subtitle
settings -> SubtitleWindow). This constructs the real widgets with the offscreen
platform plugin, drives the cinema presets, and asserts the resulting state.

Run with the project env python:

    D:/DevVmEnv/Anaconda_envs/envs/livetranslate/python.exe doc/migrations/verify_ui.py

Nothing is shown on screen; exit code 0 = all checks passed.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Must be set before QApplication is created.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PASS: list[str] = []
FAIL: list[str] = []

# The QApplication must outlive every widget. Holding it only as a local (e.g.
# returning it from a helper) lets PyQt6 collect the Python wrapper, which tears
# down the underlying C++ application and hard-crashes the next widget with no
# traceback at all.
_APP = None


def check(name: str, fn) -> None:
    try:
        detail = fn()
        PASS.append(f"{name}{f' -- {detail}' if detail else ''}")
    except Exception as exc:  # noqa: BLE001
        FAIL.append(f"{name}: {type(exc).__name__}: {exc}")
        traceback.print_exc()


def _boot():
    global _APP
    # Windows: torch must load before PyQt6 (PyTorch 2.9+ DLL conflict).
    import torch  # noqa: F401
    from PyQt6.QtWidgets import QApplication

    _APP = QApplication.instance() or QApplication([])
    return f"QApplication on platform {_APP.platformName()}"


def _config():
    import yaml

    from livetranslate.paths import CONFIG_FILE

    with open(CONFIG_FILE, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ── control panel: cinema preset -> style dict ───────────────────────────────

def _panel_presets():
    from livetranslate.ui.control_panel import ControlPanel
    from livetranslate.ui.styles import CINEMA_OVERLAY_ITEMS

    panel = ControlPanel(_config())
    assert panel._preset_keys[0] == CINEMA_OVERLAY_ITEMS[0][0], "cinema presets are not first"

    # Selecting the first cinema preset must flip pure mode and land in the dict.
    panel._style_preset.setCurrentIndex(0)
    collected = panel._collect_style()
    assert collected["preset"] == "cinema_black", collected["preset"]
    assert collected["show_meta"] is False, "cinema preset did not enable pure mode"
    assert panel._pure_mode.isChecked(), "pure-mode checkbox did not follow the preset"

    # Toggling pure mode by hand must switch the preset to Custom (last entry).
    panel._pure_mode.setChecked(False)
    assert panel._collect_style()["show_meta"] is True
    assert panel._style_preset.currentIndex() == len(panel._preset_keys) - 1, \
        "manual edit did not fall back to Custom"

    # A terminal preset must keep the metadata on.
    terminal_idx = panel._preset_keys.index("dracula")
    panel._style_preset.setCurrentIndex(terminal_idx)
    assert panel._collect_style()["show_meta"] is True, "terminal preset lost its metadata"

    return f"{len(panel._preset_keys)} presets, cinema-first, pure mode wired"


# ── subtitle settings: cinema preset -> whole window settings ────────────────

def _subwin_presets():
    from livetranslate.ui.styles import CINEMA_SUBWIN_PRESETS
    from livetranslate.ui.subtitle_settings import SubtitleSettingsWidget

    w = SubtitleSettingsWidget()
    assert w._preset_combo.itemData(0) == "cinema_outline"
    assert w._preset_combo.itemData(w._preset_combo.count() - 1) == "custom"

    w._preset_combo.setCurrentIndex(0)  # cinema_outline
    s = w.get_settings()
    assert s["bg_opacity"] == 0, f"outline preset should have no plate, got {s['bg_opacity']}"
    assert s["lines"][0]["outline_enabled"] is True
    assert s["lines"][0]["outline_width"] == 3
    assert s["lines"][1]["font_size"] == 30

    plate_idx = list(CINEMA_SUBWIN_PRESETS).index("cinema_plate")
    w._preset_combo.setCurrentIndex(plate_idx)
    s2 = w.get_settings()
    assert s2["bg_opacity"] == 110, f"plate preset opacity wrong: {s2['bg_opacity']}"
    assert s2["lines"][0]["outline_enabled"] is False

    return f"{w._preset_combo.count()} entries, outline/plate apply end-to-end"


# ── overlay: pure mode + captions-only rendering ─────────────────────────────

def _overlay_render():
    from livetranslate.ui.overlay import ChatMessage, SubtitleOverlay
    from livetranslate.ui.styles import CINEMA_OVERLAY_PRESETS

    ov = SubtitleOverlay(_config())
    ov.apply_style({**CINEMA_OVERLAY_PRESETS["cinema_black"]})
    assert ChatMessage._current_style.get("show_meta") is False, \
        "overlay style did not carry show_meta"

    ov._on_add_message(1, "12:00:00", "这是一句测试字幕。", "zh", 120.0)
    msg = ov._messages[1]
    header = msg._header_label.text()
    assert "12:00:00" not in header, f"pure mode leaked the timestamp: {header}"
    assert "ASR" not in header, f"pure mode leaked the ASR timing: {header}"
    assert "这是" in header, f"pure mode dropped the text: {header}"

    # captions mode: the translation line must disappear entirely, not show the
    # "same language" placeholder.
    ov._on_captions_only(1)
    assert not msg._trans_label.isVisible(), "captions mode left the translation line visible"

    # metadata back on -> chrome returns
    ov.apply_style({"show_meta": True})
    assert "12:00:00" in msg._header_label.text(), "leaving pure mode did not restore the chrome"

    return "pure mode hides chrome, captions mode drops the translation line"


# ── subtitle window: outline rendering path still builds ─────────────────────

def _subwin_builds():
    from livetranslate.ui.styles import CINEMA_SUBWIN_PRESETS
    from livetranslate.ui.subtitle_window import SubtitleWindow

    win = SubtitleWindow(CINEMA_SUBWIN_PRESETS["cinema_outline"])
    win.update_text("描边字幕测试", {"zh": "译文"})
    assert win._text_widgets, "subtitle window built no text widgets"
    # desired_height must never be 0 (the window would collapse)
    assert win._text_widgets[0].desired_height() > 0
    win.close()
    return f"{len(win._text_widgets)} text widget(s) built"


def _audio_source_panel():
    """The audio-source group must build cheaply and refresh on demand.

    Building it must not enumerate sessions (that is ~2.1s of blocking COM work
    and would stall the panel); the explicit refresh must then produce a real
    result, not an exception.
    """
    from livetranslate.ui.control_panel import ControlPanel

    panel = ControlPanel(_config())
    assert panel._sources_group is not None
    assert panel._sources_list.count() == 0, "panel enumerated sessions while building"
    assert panel._sources_status.text(), "no prompt shown before the first refresh"

    panel._refresh_audio_sources()
    assert panel._sources_list.count() >= 1, "refresh produced no rows"
    return f"lazy build, {panel._sources_list.count()} row(s) after refresh"


def main() -> int:
    print(f"project root: {ROOT}\n")
    check("0. boot Qt (offscreen)", _boot)
    check("1. control panel cinema presets", _panel_presets)
    check("2. subtitle-window cinema presets", _subwin_presets)
    check("3. overlay pure mode + captions-only", _overlay_render)
    check("4. subtitle window builds + renders", _subwin_builds)
    check("5. audio-source group (A) lazy build + refresh", _audio_source_panel)

    print("PASSED:")
    for p in PASS:
        print(f"  [ok]   {p}")
    if FAIL:
        print("\nFAILED:")
        for f in FAIL:
            print(f"  [FAIL] {f}")
        return 1
    print("\nall UI checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
