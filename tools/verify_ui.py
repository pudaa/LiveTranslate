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

import copy
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


def _release_all(win, frame=0.016, max_frames=600):
    """Drive the paced release to completion, as the animation timer would.

    update_partial() only buffers now, so anything asserting on what is on screen
    has to run the release first.
    """
    for _ in range(max_frames):
        if win._release_step(frame):
            win._render_marquee()
        if not win._release_queue:
            return


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

    # Pacing: a minimum display time is implemented by *deferring* the next
    # replacement. That is right for wrapped lines and wrong for a marquee, where
    # the text is already on screen as the in-progress line -- deferring only
    # makes the committed line arrive late.
    assert win._scroll_line_config() is not None
    win._last_insert_time = time.monotonic() * 1000
    win.update_text("紧接着的一句", {})
    assert not win._pending_segment_timers, "marquee mode deferred the commit"

    wrapped_cfg = copy.deepcopy(DEFAULT_SUBTITLE_WIN_SETTINGS)
    for line_cfg in wrapped_cfg["lines"]:
        line_cfg["scroll"] = False
    wrapped = SubtitleWindow(wrapped_cfg)
    wrapped.show()
    _APP.processEvents()
    wrapped._last_insert_time = time.monotonic() * 1000
    wrapped.update_text("紧接着的一句", {})
    assert wrapped._pending_segment_timers, "wrapped mode lost its pacing"
    for timer in wrapped._pending_segment_timers:
        timer.stop()
    wrapped.close()

    win.close()
    return (
        f"1 line when untranslated ({one_line_h}px), 2 when translated "
        f"({two_line_h}px); PNGs in logs/"
    )


def _subtitle_partial_line():
    """The in-progress sentence must grow in place, not arrive once finished."""
    from livetranslate.paths import LOGS_DIR
    from livetranslate.ui.subtitle_window import DEFAULT_SUBTITLE_WIN_SETTINGS, SubtitleWindow

    win = SubtitleWindow(dict(DEFAULT_SUBTITLE_WIN_SETTINGS))
    win.show()
    _APP.processEvents()

    win.update_partial("那么第三个标准")
    _release_all(win)
    assert win._released_tail == "那么第三个标准", win._released_tail

    win.update_partial("那么第三个标准的划分叫做根据教育功能")
    _release_all(win)
    assert win._released_tail.endswith("教育功能"), win._released_tail
    shown = win._text_widgets[0]._text
    assert shown.count("那么第三个标准") == 1, f"the tail accumulated instead of replacing: {shown}"
    win._fit_height_snap()
    _APP.processEvents()
    win.grab().save(str(LOGS_DIR / "subtitle-partial.png"))

    # Committing the sentence must clear the partial and show it exactly once.
    win._last_insert_time = 0.0
    win.update_text("那么第三个标准的划分叫做根据教育功能所呈现的方式的不同", {})
    _APP.processEvents()
    assert win._partial == "", "partial survived the commit"
    committed = win._text_widgets[0]._text
    assert committed.count("那么第三个标准") == 1, f"duplicated after commit: {committed}"
    win.close()
    return "partial replaces in place, then clears on commit without duplicating"


def _subtitle_marquee():
    """The original line must scroll as one row, decaying by width not by count."""
    import time as _time

    from livetranslate.ui.subtitle_window import DEFAULT_SUBTITLE_WIN_SETTINGS, SubtitleWindow

    win = SubtitleWindow(dict(DEFAULT_SUBTITLE_WIN_SETTINGS))
    win.show()
    _APP.processEvents()
    assert win._scroll_line_config() is not None, "no scroll-mode line is configured"
    tw = win._text_widgets[0]
    assert tw._scroll_mode, "the original line is not in marquee mode"

    one_row = None
    long_text = "那么第三个标准的划分叫做根据教育功能所呈现的方式的不同"
    win._last_insert_time = 0.0
    win._insert_sentence(long_text * 4, {})
    _APP.processEvents()
    one_row = tw.desired_height()
    assert one_row > 0, "the marquee rendered nothing"
    assert len(tw._marquee) == 1, tw._marquee

    # Nothing wraps: adding much more text must not add a row.
    win._insert_sentence(long_text * 4, {})
    _APP.processEvents()
    assert tw.desired_height() == one_row, "the line wrapped onto a second row"

    # Overflow is dropped from the *front*, by width -- not by expiring whole
    # sentences, which is what used to make the display jump between rows.
    win._fit_height_snap()
    _APP.processEvents()
    shown = "".join(t for t, _ in tw._marquee)
    assert shown, "everything was trimmed"
    assert len(shown) < len(long_text * 4), "nothing was trimmed"
    assert shown == (long_text * 4)[-len(shown):], "trimming did not take from the front"
    assert "|" not in shown, "a pipe separator is still drawn"

    # A pause shows up as blank width, and is capped so a long silence cannot
    # scroll the whole line away.
    cap = DEFAULT_SUBTITLE_WIN_SETTINGS["pause_gap_max_px"]
    win._last_text_at = _time.monotonic() - 60.0
    assert win._pause_gap_px() == cap, "the pause gap is not capped"

    win.close()
    return f"one row ({one_row}px), trimmed to {len(shown)} chars from the front, gap cap {cap}px"


def _subtitle_marquee_animation():
    """The pause must stop widening once speech resumes, and text must slide in."""
    import time as _time

    from livetranslate.ui.subtitle_window import DEFAULT_SUBTITLE_WIN_SETTINGS, SubtitleWindow

    win = SubtitleWindow(dict(DEFAULT_SUBTITLE_WIN_SETTINGS))
    win.show()
    _APP.processEvents()

    win._last_insert_time = 0.0
    win._insert_sentence("第一句话", {})
    win._last_text_at = _time.monotonic() - 1.0
    gap_silent = win._pause_gap_px()
    assert gap_silent > 0, "silence produced no gap at all"

    # Speech resumes: the gap has to freeze. A gap that keeps widening while the
    # next sentence is on screen reads as talking and pausing at once.
    win.update_partial("第二句")
    _APP.processEvents()
    frozen = win._pause_gap_px()
    win._last_text_at = _time.monotonic() - 5.0  # pretend more silence passed
    assert win._pause_gap_px() == frozen, (
        "the gap kept widening after the next sentence had already appeared"
    )

    # A *large* jump is parked off the right edge and eased in. A single released
    # unit deliberately is not -- that happens several times a second and would
    # read as the caption perpetually sliding itself back in (check 14).
    win._slide_px = 0.0
    win._last_text_width = 0.0
    win.update_partial("第二句话变得更长了一些还要继续往下说很长一段")
    win._release_step(0.016)
    assert win._slide_px == 0.0, "a single released unit was parked off the edge"
    win._flush_release()
    win._compensate_insertion()
    parked = win._slide_px
    assert parked > 0, "a large burst was not parked"

    # ...and that offset decays, so the line travels instead of jumping.
    win._last_tick = _time.monotonic() - 0.05
    win._tick_marquee()
    assert win._slide_px < parked, "the insertion offset did not animate down"

    win.close()
    return f"gap froze at {frozen:.0f}px; a {parked:.0f}px burst eases in, units do not"


def _subtitle_no_blank_frames():
    """The caption must never go blank mid-utterance, and a commit must not lurch.

    main.py cleared the in-progress line *before* committing, leaving a frame with
    neither a partial nor committed text. Since the commit can be deferred by
    min_display_ms, the caption stayed blank for up to 1.5s and then slid back in
    from the right -- which is exactly what made the animation unusable. The
    repeated 0 <-> N width jumps also fired the insertion compensation on every
    sentence, lurching the line sideways.
    """
    from livetranslate.ui.subtitle_window import DEFAULT_SUBTITLE_WIN_SETTINGS, SubtitleWindow

    settings = dict(DEFAULT_SUBTITLE_WIN_SETTINGS)
    settings["min_display_ms"] = 1500  # the value that made the blank last
    win = SubtitleWindow(settings)
    win.show()
    _APP.processEvents()
    tw = win._text_widgets[0]

    started = False
    blanks = []
    for sentence in (
        "那么第三个标准的划分叫做根据教育功能",
        "分成显性和隐性两种类型",
        "第一种是显性课程",
    ):
        for event in ("partial-short", "partial-full", "commit"):
            if event == "commit":
                win.update_text(sentence, {})
            else:
                win.update_partial(sentence[:8] if event == "partial-short" else sentence)
            _APP.processEvents()
            rendered = len(tw._text)
            if rendered:
                started = True
            elif started:
                blanks.append(f"{event}:{sentence[:6]}")
    assert not blanks, f"the caption went blank mid-utterance: {blanks}"

    # Committing the sentence already on screen as the in-progress line must not
    # change the line's width, or the insertion animation fires and the caption
    # jumps sideways for no reason the viewer can see.
    win.update_partial("一句话正在显示")
    _APP.processEvents()
    win._slide_px = 0.0
    win.update_text("一句话正在显示", {})
    _APP.processEvents()
    assert win._slide_px == 0.0, f"committing added a {win._slide_px:.0f}px lurch"

    win.close()
    return "no blank frame across 9 partial/commit events; commit adds no lurch"


def _subtitle_paced_release():
    """ASR phrases must go up one unit at a time, and still keep up.

    The recogniser delivers a whole phrase per interval, so it lands as one lump
    whose size varies and whose start the reader cannot locate without re-scanning
    the line. Buffering it and releasing one reading unit at a time makes every
    addition the same size and puts it in the same place; the adaptive rate is
    what stops the buffer from growing, so the caption stays real-time.
    """
    from livetranslate.ui.subtitle_window import (
        DEFAULT_SUBTITLE_WIN_SETTINGS,
        SubtitleWindow,
        split_marquee_units,
    )

    win = SubtitleWindow(dict(DEFAULT_SUBTITLE_WIN_SETTINGS))
    win.show()
    _APP.processEvents()

    frame, interval = 0.016, 0.6
    releases, settled_depths = [], []
    phrase = ""
    for index in range(8):
        phrase += "那么第三个标准"  # 7 units arrive every interval
        win.update_partial(phrase)
        for _ in range(int(interval / frame)):
            before = len(split_marquee_units(win._released_tail))
            win._release_step(frame)
            after = len(split_marquee_units(win._released_tail))
            if after > before:
                releases.append(after - before)
        # Measured at the *end* of an interval: straight after an arrival the
        # whole increment is queued, which says nothing about whether it keeps up.
        if index:
            settled_depths.append(len(split_marquee_units(win._release_queue)))

    assert releases, "nothing was ever released"
    assert set(releases) == {1}, f"releases were not one unit each: {sorted(set(releases))}"
    assert max(settled_depths) <= 4, (
        f"the release fell behind: queue settled at {max(settled_depths)} units"
    )

    win.close()
    return (
        f"all {len(releases)} releases were 1 unit; "
        f"queue settles at {max(settled_depths)} (7 arrive per interval)"
    )


def _subtitle_anchor_and_revision():
    """The bar must not move when the text grows, and a correction must replace.

    Two behaviours that were both wrong: the window was centred on every height
    change, so a position the user set never held; and a revised partial was
    re-queued in full, so words already on screen appeared a second time.
    """
    from livetranslate.ui.subtitle_window import DEFAULT_SUBTITLE_WIN_SETTINGS, SubtitleWindow

    win = SubtitleWindow(dict(DEFAULT_SUBTITLE_WIN_SETTINGS))
    win.show()
    _APP.processEvents()
    win.move(200, 300)
    win._fit_height_snap()
    _APP.processEvents()
    bottom = win.y() + win.height()

    win._last_insert_time = 0.0
    win.update_text("那么第三个标准的划分叫做根据教育功能分成显性和隐性两种", {})
    win._fit_height_snap()
    _APP.processEvents()
    assert win.y() + win.height() == bottom, (
        f"the bar moved: bottom {bottom} -> {win.y() + win.height()}"
    )

    # A revised partial replaces the in-progress text instead of re-showing it.
    win.update_partial("教育价值它是教育应该干什么")
    _release_all(win)
    first = win._released_tail
    win.update_partial("教育价值他是教育应该干什么")  # 它 -> 他
    _release_all(win)
    assert win._released_tail == "教育价值他是教育应该干什么", win._released_tail
    assert win._released_tail.count("教育价值") == 1, (
        f"the correction re-showed the text already on screen: {win._released_tail}"
    )
    assert first != win._released_tail

    # A single released unit must not be parked off the right edge: with paced
    # release that happens several times a second, and it reads as the caption
    # perpetually sliding itself back in.
    win._slide_px = 0.0
    win._last_text_width = 0.0
    win.update_partial("教育价值他是教育应该干什么呢")
    _release_all(win)
    assert win._slide_px == 0.0, f"a one-unit release was parked ({win._slide_px:.0f}px)"

    # The width must hold for the *whole* correction, not just at its end. The
    # replacement types in behind reserved space that is consumed as it arrives,
    # so the line does not move at any point in the operation.
    from PyQt6.QtGui import QFontMetrics

    tw = win._text_widgets[0]
    line_width = lambda: QFontMetrics(tw._font).horizontalAdvance(
        "".join(t for t, _ in tw._marquee)
    )
    win.clear()
    win.update_partial("教育价值它是教育应该干什么啊")
    _release_all(win)
    before = line_width()
    win.update_partial("教育价值他是教育应该干什么")  # one character shorter
    widths = [line_width()]
    for _ in range(120):
        if win._release_step(0.016):
            win._render_marquee()
        widths.append(line_width())
        if not win._release_queue:
            break
    distinct = sorted({round(w) for w in widths})
    assert len(distinct) == 1, f"the width varied during the correction: {distinct}"
    assert abs(widths[-1] - before) < 0.5, f"ended {widths[-1] - before:+.0f}px off"

    # The padding must sit inside the line, not trail at the right edge where it
    # reads as the caption failing to keep up.
    tail = "".join(t for t, _ in tw._marquee)
    assert not tail.endswith("\u3000"), "padding is trailing at the right edge"
    assert "\u3000" in tail, "a shorter correction left no padding at all"

    # A corrected unit materialises from a partly visible value, not from nothing.
    # Starting from nothing is the cue that means "this is new", and confusing the
    # two is exactly what the marking is meant to avoid.
    assert tw._correction_fade(0.0) > 0.2, "a correction starts from invisible"
    assert tw._correction_fade(0.0) < 0.9, "a correction is not visibly gentle"
    assert tw._correction_fade(10.0) >= 0.999, "a correction never reaches full strength"

    win.close()
    return (
        "bottom edge held; revision replaced in place; one-unit release not parked; "
        f"{before:.0f}px held through a 3-character correction"
    )


def _short_utterance_threshold():
    """The fragment heuristic must be script-aware."""
    # main.py lives at the project root, not inside the package.
    sys.path.insert(0, str(ROOT))
    from main import LiveTranslateApp as App

    # Six CJK characters is a complete clause, not a fragment -- treating it as
    # one buffered most of a Chinese lecture and committed it in repeated lumps.
    assert not App._is_short_utterance("而应该干什么？"), "a CJK clause was treated as a fragment"
    assert not App._is_short_utterance("是一个应然的结果。")
    assert App._is_short_utterance("好"), "a filler was not filtered"
    assert App._is_short_utterance("嗯")
    # Latin keeps the original threshold.
    assert App._is_short_utterance("the")
    assert App._is_short_utterance("and then")
    assert not App._is_short_utterance("and then we go")
    return "CJK clauses kept; fillers filtered; Latin unchanged"


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
    check("9. subtitle in-progress line grows then commits once", _subtitle_partial_line)
    check("10. subtitle marquee: one row, trims from the front", _subtitle_marquee)
    check("11. marquee gap freezes; new text slides in", _subtitle_marquee_animation)
    check("12. no blank frame; commit adds no lurch", _subtitle_no_blank_frames)
    check("13. ASR text released one unit at a time", _subtitle_paced_release)
    check("14. bottom-anchored; revision replaces; no park", _subtitle_anchor_and_revision)
    check("15. fragment heuristic is script-aware", _short_utterance_threshold)

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
