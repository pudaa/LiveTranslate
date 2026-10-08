"""Tests for the SRT writer's timing rules.

An SRT file is only useful if it lines up with the video, so the invariants that
matter are: monotonic, non-overlapping, never shorter than a readable flash, and
never renumbered.
"""

import re

from livetranslate.output.srt import MIN_DURATION, SRTWriter, format_timestamp

TIMESTAMP_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2}),(\d{3})$")


def _parse(ts: str) -> float:
    m = TIMESTAMP_RE.match(ts)
    assert m, f"malformed SRT timestamp: {ts!r}"
    h, mi, s, ms = (int(g) for g in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000


def _cue_times(path):
    """Yield (start, end) pairs from a written SRT file."""
    text = path.read_text(encoding="utf-8")
    for block in text.strip().split("\n\n"):
        lines = block.split("\n")
        start, end = lines[1].split(" --> ")
        yield _parse(start), _parse(end)


# ── timestamp formatting ─────────────────────────────────────────────────────

def test_format_timestamp_pads_and_rounds():
    assert format_timestamp(0) == "00:00:00,000"
    assert format_timestamp(1.5) == "00:00:01,500"
    assert format_timestamp(61.25) == "00:01:01,250"
    assert format_timestamp(3661.001) == "01:01:01,001"


def test_format_timestamp_clamps_negatives():
    assert format_timestamp(-5) == "00:00:00,000"


# ── writing ──────────────────────────────────────────────────────────────────

def test_writes_sequential_cues(tmp_path):
    w = SRTWriter(tmp_path, enabled=True)
    w.write(0.0, 2.0, "first line")
    w.write(2.5, 4.0, "second line")
    w.close()

    text = w.path.read_text(encoding="utf-8")
    assert text.startswith("1\n00:00:00,000 --> 00:00:02,000\nfirst line\n")
    assert "2\n00:00:02,500 --> 00:00:04,000\nsecond line\n" in text


def test_disabled_writes_nothing(tmp_path):
    w = SRTWriter(tmp_path, enabled=False)
    w.write(0.0, 2.0, "ignored")
    w.close()
    assert w.path is None
    assert not list(tmp_path.glob("*.srt"))


def test_short_cue_is_extended_to_min_duration(tmp_path):
    w = SRTWriter(tmp_path, enabled=True)
    w.write(10.0, 10.1, "blink")
    w.close()
    start, end = next(_cue_times(w.path))
    assert end - start >= MIN_DURATION


def test_cues_never_overlap(tmp_path):
    """A late `start` must be pushed past the previous cue's end, and the cue
    after that must still be monotonic."""
    w = SRTWriter(tmp_path, enabled=True)
    w.write(0.0, 5.0, "long cue")
    w.write(1.0, 2.0, "starts inside the previous one")
    w.write(6.0, 7.0, "later")
    w.close()

    times = list(_cue_times(w.path))
    assert len(times) == 3
    for (_, prev_end), (start, _) in zip(times, times[1:]):
        assert start >= prev_end, f"overlap: {times}"


def test_empty_text_is_skipped(tmp_path):
    w = SRTWriter(tmp_path, enabled=True)
    w.write(0.0, 2.0, "   ")
    w.write(2.0, 4.0, "")
    w.write(4.0, 6.0, "real")
    w.close()

    text = w.path.read_text(encoding="utf-8")
    assert text.startswith("1\n"), "index should not advance for empty cues"
    assert "real" in text


def test_reversed_times_are_repaired(tmp_path):
    w = SRTWriter(tmp_path, enabled=True)
    w.write(5.0, 2.0, "end before start")
    w.close()
    start, end = next(_cue_times(w.path))
    assert end > start


def test_translation_becomes_a_second_line(tmp_path):
    w = SRTWriter(tmp_path, enabled=True)
    w.write(0.0, 2.0, "hello", "你好")
    w.close()
    text = w.path.read_text(encoding="utf-8")
    assert "hello\n你好\n" in text


def test_toggle_after_construction(tmp_path):
    w = SRTWriter(tmp_path, enabled=False)
    assert not w.is_enabled()
    w.set_enabled(True)
    w.write(0.0, 2.0, "enabled later")
    w.close()
    assert "enabled later" in w.path.read_text(encoding="utf-8")
