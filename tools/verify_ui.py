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
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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


def _capture_target():
    """Selecting a row must set the capture target; system rows must be refused.

    The system-sounds session has pid 0 and no process, so it cannot be a
    process-loopback target -- picking it silently would leave the user
    believing they had isolated something when they had not.
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QListWidgetItem

    from livetranslate.ui.control_panel import ControlPanel

    panel = ControlPanel(_config())
    panel._current_settings["audio_target_process"] = ""
    panel._update_audio_target_label()
    assert panel._sources_target.text(), "no 'capture everything' label"

    panel._sources_list.clear()
    panel._sources_list.addItem(QListWidgetItem("system sounds row"))
    panel._sources_list.setCurrentRow(0)
    panel._capture_selected_process()
    assert not panel._current_settings.get("audio_target_process"), \
        "a row with no process became the capture target"

    row = QListWidgetItem("vlc.exe row")
    row.setData(Qt.ItemDataRole.UserRole, "vlc.exe")
    panel._sources_list.addItem(row)
    panel._sources_list.setCurrentItem(row)
    panel._capture_selected_process()
    assert panel._current_settings["audio_target_process"] == "vlc.exe"
    assert "vlc.exe" in panel._sources_target.text(), panel._sources_target.text()

    panel._set_audio_target("")
    assert panel._current_settings["audio_target_process"] == ""
    return "select sets target, system row refused, clear restores device-wide"


def _wizard_skips_cached_models():
    """A first run must not re-download models that are already cached.

    The setup wizard is triggered by a missing settings file, not by missing
    models, so without an explicit check it always entered the download path --
    a pre-fetched machine still showed "Downloading ...", which reads as "why is
    it downloading again?".
    """
    from livetranslate.models.manager import get_missing_models
    from livetranslate.ui.dialogs import SetupWizardDialog

    if get_missing_models("funasr", "sensevoice-small", "ms"):
        return "skipped (models are not cached on this machine)"

    wizard = SetupWizardDialog()
    wizard._download_worker("ms", "none")
    assert wizard._skipped, "wizard entered the download path despite a full cache"
    assert wizard._error is None, wizard._error
    return "cache detected before the network was touched"


def _subtitle_lines():
    """Same-language / captions mode must render ONE line, not two identical ones.

    main.py used to pass the ASR text as its own translation, so the subtitle
    window drew the original and then the "translation" line with the same text
    at a different size. It also renders a PNG so the result can be eyeballed.
    """
    from livetranslate.paths import LOGS_DIR
    from livetranslate.ui.subtitle_window import DEFAULT_SUBTITLE_WIN_SETTINGS, SubtitleWindow

    win = SubtitleWindow(dict(DEFAULT_SUBTITLE_WIN_SETTINGS))
    win.show()
    _APP.processEvents()

    orig_w, trans_w = win._text_widgets[0], win._text_widgets[1]

    # Same-language case: no translation available.
    win.update_text("那么第三个标准的划分叫做根据教育功能所呈现的方式的不同", {})
    _APP.processEvents()
    assert orig_w._text, "original line is empty"
    assert not trans_w._text, f"translation line rendered the original again: {trans_w._text!r}"
    assert trans_w.desired_height() == 0, "unused line still reserves height"

    one_line_h = win._calc_target_height()
    win._fit_height_snap()  # settle the layout; the animated fit would be mid-flight
    _APP.processEvents()
    win.grab().save(str(LOGS_DIR / "subtitle-one-line.png"))

    # Translation present: two lines, and the window grows by one line only.
    # Reset the pacing clock first: _on_update_text delays a replacement until
    # _min_display_ms has elapsed, and this assertion is about rendering, not
    # pacing (which is asserted separately below).
    win._last_insert_time = 0.0
    win.update_text(
        "那么第三个标准的划分叫做根据教育功能所呈现的方式的不同", {"zh": "译文示例文本"}
    )
    _APP.processEvents()
    assert orig_w._text and trans_w._text, "translated case lost a line"
    assert trans_w.desired_height() > 0
    two_line_h = win._calc_target_height()
    assert two_line_h > one_line_h, "window height did not grow for the second line"
    win._fit_height_snap()
    _APP.processEvents()
    win.grab().save(str(LOGS_DIR / "subtitle-two-lines.png"))

    # Pacing: an update arriving immediately after the previous one is *delayed*
    # rather than applied, which is what makes live captions lag the audio.
    win._last_insert_time = time.monotonic() * 1000
    win.update_text("紧接着的一句", {})
    assert win._pending_segment_timers, "update was not paced"
    for timer in win._pending_segment_timers:
        timer.stop()

    win.close()
    return (
        f"1 line when untranslated ({one_line_h}px), 2 when translated "
        f"({two_line_h}px); PNGs in logs/"
    )


def main() -> int:
    print(f"project root: {ROOT}\n")
    check("0. boot Qt (offscreen)", _boot)
    check("1. control panel cinema presets", _panel_presets)
    check("2. subtitle-window cinema presets", _subwin_presets)
    check("3. overlay pure mode + captions-only", _overlay_render)
    check("4. subtitle window builds + renders", _subwin_builds)
    check("5. audio-source group (A) lazy build + refresh", _audio_source_panel)
    check("6. capture target selection (C wiring)", _capture_target)
    check("7. setup wizard skips cached models", _wizard_skips_cached_models)
    check("8. subtitle lines: no duplicate, no blank gap", _subtitle_lines)

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
