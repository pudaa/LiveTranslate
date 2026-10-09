"""Tests for the audio-session enumeration logic (P4.3, the "A" half).

The COM calls themselves need real hardware, so these cover the pure parts that
decide what the user is told: name matching between the two device-name
dialects, session labels, and what the diagnosis concludes from a snapshot.

`diagnose()` is the piece that matters most: it is what tells the user whether
their capture is actually isolated, and getting it wrong means confidently
reassuring them while they caption the whole system.
"""

import pytest

from livetranslate.audio import sessions as S


# ── name matching ────────────────────────────────────────────────────────────

def test_name_key_matches_the_two_device_name_dialects():
    """PortAudio and the endpoint API spell the same device differently."""
    assert S._name_key("扬声器 (Realtek(R) Audio)") == S._name_key(
        "扬声器（Realtek Audio）"
    )
    assert S._name_key("Speakers (Realtek Audio)") == S._name_key(
        "speakers (realtek audio)"
    )


def test_name_key_separates_different_devices():
    assert S._name_key("扬声器 (Realtek(R) Audio)") != S._name_key(
        "耳机 (EDIFIER Fit900NB)"
    )


# ── session labels ───────────────────────────────────────────────────────────

def test_system_session_uses_a_sentinel_not_a_language_string():
    s = S.SessionInfo(pid=0, process="", display_name="")
    assert s.label == S.SYSTEM_LABEL


def test_label_prefers_the_process_name():
    s = S.SessionInfo(pid=42, process="vlc.exe", display_name="VLC media player")
    assert s.label == "vlc.exe"


def test_label_falls_back_when_the_process_is_gone():
    s = S.SessionInfo(pid=42, process="", display_name="Some App")
    assert s.label == "Some App"
    assert S.SessionInfo(pid=42, process="", display_name="").label == "pid 42"


def test_display_name_drops_unresolved_resource_references():
    # Windows returns this for system sounds instead of a readable name.
    assert S._display_name("@%SystemRoot%\\System32\\AudioSrv.Dll,-202") == ""
    assert S._display_name("VLC media player") == "VLC media player"


# ── playing detection ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "peak,expected",
    [(0.0, False), (0.0009, False), (0.001, False), (0.002, True), (0.8, True)],
)
def test_peak_threshold_separates_silence_from_playback(peak, expected):
    s = S.SessionInfo(pid=1, process="a.exe", peak=peak)
    assert s.is_playing is expected


def test_endpoint_playing_filters_idle_sessions():
    ep = S.EndpointSessions(
        name="Speakers",
        sessions=[
            S.SessionInfo(pid=1, process="a.exe", peak=0.5),
            S.SessionInfo(pid=2, process="b.exe", peak=0.0),
        ],
    )
    assert [s.label for s in ep.playing] == ["a.exe"]


# ── diagnosis ────────────────────────────────────────────────────────────────

def _patch(monkeypatch, by_endpoint, default="Speakers"):
    monkeypatch.setattr(S, "list_by_endpoint", lambda: by_endpoint)
    monkeypatch.setattr(S, "default_endpoint_name", lambda: default)


def test_single_source_on_the_captured_endpoint_is_ok(monkeypatch):
    """One app on the captured endpoint means the mix *is* that app."""
    _patch(
        monkeypatch,
        [S.EndpointSessions("Speakers", [S.SessionInfo(1, "vlc.exe", peak=0.4)])],
    )
    d = S.diagnose("Speakers")
    assert d.level == "ok"
    assert d.captured == "Speakers"
    assert d.playing == ("vlc.exe",)


def test_two_sources_on_the_captured_endpoint_warns(monkeypatch):
    _patch(
        monkeypatch,
        [
            S.EndpointSessions(
                "Speakers",
                [S.SessionInfo(1, "vlc.exe", peak=0.4), S.SessionInfo(2, "spotify.exe", peak=0.3)],
            )
        ],
    )
    d = S.diagnose("Speakers")
    assert d.level == "warn"
    assert set(d.playing) == {"vlc.exe", "spotify.exe"}


def test_nothing_playing_on_the_captured_endpoint_is_ok(monkeypatch):
    _patch(
        monkeypatch,
        [
            S.EndpointSessions("Speakers", [S.SessionInfo(1, "a.exe", peak=0.0)]),
            S.EndpointSessions("Headset", [S.SessionInfo(2, "music.exe", peak=0.9)]),
        ],
    )
    d = S.diagnose("Speakers")
    assert d.level == "ok"
    assert d.playing == ()
    assert d.other_playing == ("music.exe",)


def test_isolation_is_reported_in_other_playing(monkeypatch):
    """An app routed to a second endpoint must not count as a conflict."""
    _patch(
        monkeypatch,
        [
            S.EndpointSessions("Speakers", [S.SessionInfo(1, "boom.exe", peak=0.3)]),
            S.EndpointSessions("CABLE Input", [S.SessionInfo(2, "vlc.exe", peak=0.6)]),
        ],
        default="Speakers",
    )
    d = S.diagnose("CABLE Input")
    assert d.level == "ok"
    assert d.playing == ("vlc.exe",)
    assert d.other_playing == ("boom.exe",)


def test_unknown_device_falls_back_to_the_real_default(monkeypatch):
    """A stale saved device name must not be mistaken for the captured one."""
    _patch(
        monkeypatch,
        [S.EndpointSessions("Speakers", [S.SessionInfo(1, "a.exe", peak=0.5)])],
        default="Speakers",
    )
    d = S.diagnose("Some Unplugged Headset")
    assert d.captured == "Speakers"
    assert d.playing == ("a.exe",)


def test_no_endpoints_is_unknown_not_ok(monkeypatch):
    """'pycaw unavailable' must not be reported as a clean bill of health."""
    _patch(monkeypatch, [])
    assert S.diagnose(None).level == "unknown"


def test_error_endpoints_do_not_count_towards_the_usable_total(monkeypatch):
    _patch(
        monkeypatch,
        [
            S.EndpointSessions("Speakers", [S.SessionInfo(1, "a.exe", peak=0.5)]),
            S.EndpointSessions("Dead HDMI", error="AUDCLNT_E_DEVICE_INVALIDATED"),
        ],
    )
    d = S.diagnose("Speakers")
    assert d.endpoint_count == 1
    assert d.level == "ok"


# ── endpoint filtering ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "name", ["Speakers (Realtek Audio) Loopback", "立体声混音 (Realtek Audio)", "Stereo Mix"]
)
def test_loopback_taps_are_ignored(name):
    assert S._looks_like_tap(name)


def test_normal_endpoints_are_not_treated_as_taps():
    assert not S._looks_like_tap("扬声器 (Realtek(R) Audio)")
    assert not S._looks_like_tap("CABLE Input (VB-Audio Virtual Cable)")
