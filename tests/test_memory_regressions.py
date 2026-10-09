"""Regression tests for the long-session memory fixes (P4.2).

Each case here is a leak that only shows up after a long session, which is
exactly why they need pinning: a five-minute manual run looks perfectly fine.

  1. the mic buffer had no ceiling -- when the loopback stream goes down nothing
     drains it, so it grew for the whole outage (and np.concatenate per append
     made that quadratic)
  2. the overlay's rotating message cap only bounded the *layout*: a parentless
     QTimer holding a bound-method connection kept every evicted ChatMessage
     alive, so memory grew regardless of the cap
  3. eviction used bare deleteLater(), leaving the widget parented until the
     event loop got to it
"""

import os

import numpy as np
import pytest

# ── 1. mic buffer ceiling ────────────────────────────────────────────────────

def _bare_capture(sample_rate: int = 16000):
    """An AudioCapture with __init__ skipped.

    Constructing the real thing opens a PortAudio instance and probes the system
    default device; these tests only exercise the buffering arithmetic.
    """
    from livetranslate.audio.capture import AudioCapture

    cap = AudioCapture.__new__(AudioCapture)
    cap.sample_rate = sample_rate
    cap._mic_buf = np.array([], dtype=np.float32)
    return cap


def test_mic_buffer_stays_bounded_when_nothing_drains_it():
    from livetranslate.audio.capture import MIC_BUFFER_MAX_SECONDS

    cap = _bare_capture()
    chunk = np.ones(8000, dtype=np.float32)  # 0.5s at 16 kHz
    for _ in range(40):  # would be 20s unbounded
        cap._mic_buffer_push(chunk)

    cap_samples = int(16000 * MIC_BUFFER_MAX_SECONDS)
    assert cap._mic_buf.size <= cap_samples, (
        f"mic buffer grew to {cap._mic_buf.size} samples, cap is {cap_samples}"
    )


def test_mic_buffer_drops_the_oldest_audio_first():
    """The newest samples are the ones that line up with the loopback chunk."""
    from livetranslate.audio.capture import MIC_BUFFER_MAX_SECONDS

    cap = _bare_capture()
    cap._mic_buffer_push(np.full(40000, 1.0, dtype=np.float32))  # old, over cap
    cap._mic_buffer_push(np.full(8000, 2.0, dtype=np.float32))   # newest

    assert cap._mic_buf.size <= int(16000 * MIC_BUFFER_MAX_SECONDS)
    # The tail must still be the newest chunk.
    assert cap._mic_buf[-8000:].min() == pytest.approx(2.0)


def test_mic_buffer_keeps_the_newest_audio_of_an_oversized_chunk():
    """A single chunk larger than the cap is trimmed, not discarded.

    Keeping the newest samples (and dropping the oldest) is what matters: those
    are the ones that line up with the loopback chunk about to be mixed in.
    """
    from livetranslate.audio.capture import MIC_BUFFER_MAX_SECONDS

    cap = _bare_capture()
    cap_samples = int(16000 * MIC_BUFFER_MAX_SECONDS)
    huge = np.ones(cap_samples * 2, dtype=np.float32)
    huge[-8000:] = 5.0  # marker on the newest audio

    cap._mic_buffer_push(huge)

    assert cap._mic_buf.size == cap_samples
    assert cap._mic_buf[-1] == pytest.approx(5.0)


# ── 2/3. overlay message lifetime ────────────────────────────────────────────

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Must be module-level: PyQt6 collects a function-local QApplication wrapper,
# which destroys the C++ application and hard-crashes the next widget built.
_APP = None


def _qt_app():
    global _APP
    if _APP is not None:
        return _APP
    import torch  # noqa: F401  -- torch must precede PyQt6 on Windows

    from PyQt6.QtWidgets import QApplication

    _APP = QApplication.instance() or QApplication([])
    return _APP


def _overlay(max_messages: int = 3):
    import yaml

    from livetranslate.paths import CONFIG_FILE
    from livetranslate.ui.overlay import SubtitleOverlay

    _qt_app()
    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    ov = SubtitleOverlay(cfg)
    ov._max_messages = max_messages
    return ov


def test_streaming_timer_is_parented_to_its_message():
    """A parentless timer would outlive the message via its bound-method slot."""
    from livetranslate.ui.overlay import ChatMessage

    _qt_app()
    msg = ChatMessage(1, "12:00:00", "hello", "zh", 100.0)
    msg.update_streaming("partial")
    assert msg._streaming_timer.parent() is msg


def test_prepare_for_removal_detaches_and_stops_the_timer():
    from livetranslate.ui.overlay import ChatMessage

    _qt_app()
    msg = ChatMessage(1, "12:00:00", "hello", "zh", 100.0)
    msg.update_streaming("partial")
    assert msg._streaming_timer.isActive()

    msg.prepare_for_removal()
    assert msg.parent() is None, "widget is still parented, so still painted/allocated"
    assert not msg._streaming_timer.isActive()


def test_eviction_detaches_every_dropped_message():
    from livetranslate.ui.overlay import ChatMessage

    ov = _overlay(max_messages=3)

    detached = []
    original = ChatMessage.prepare_for_removal

    def spy(self):
        detached.append(self)
        return original(self)

    ChatMessage.prepare_for_removal = spy
    try:
        for i in range(1, 6):  # overfill -> 1 and 2 get evicted
            ov._on_add_message(i, "12:00:00", f"line {i}", "zh", 100.0)
    finally:
        ChatMessage.prepare_for_removal = original

    assert sorted(ov._messages) == [3, 4, 5]
    assert len(detached) == 2, "evicted messages were not detached before deleteLater()"
    assert all(m.parent() is None for m in detached)


def test_clear_detaches_every_message():
    ov = _overlay(max_messages=50)
    for i in range(1, 4):
        ov._on_add_message(i, "12:00:00", f"line {i}", "zh", 100.0)
    widgets = list(ov._messages.values())

    ov._on_clear()

    assert ov._messages == {}
    assert all(w.parent() is None for w in widgets)
