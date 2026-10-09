"""Per-application audio session enumeration -- the "A" half of audio-source filtering.

Why this module exists
----------------------
WASAPI loopback captures an **endpoint mix**, not an application. There is no
supported way to pull one process's audio back out of that mix short of
*process loopback* (``AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK``), which
means hand-rolling ``ActivateAudioInterfaceAsync`` through comtypes. So the
practical route to captioning just one player is:

  **A. enumerate** the sessions, so you can see which app is making noise and --
     because sessions are enumerated *per endpoint* -- confirm where it is being
     rendered.
  **B. isolate** by routing that app to a *different* output endpoint than the
     one we capture.

B needs a second endpoint. With only one, an app cannot be isolated and
everything on that endpoint gets captioned together. :func:`diagnose` says that
out loud instead of leaving the user to guess.

Everything here is **read-only**: enumerating sessions and reading peak meters
never changes what the user hears.

Why not pycaw's helper
----------------------
``AudioUtilities.GetAllSessions()`` only covers the *default* endpoint, and
pycaw's ``AudioSession`` does not expose a meter. This module walks
``IAudioSessionManager2`` per endpoint and queries the interfaces directly, so
one pass answers both "who is playing" and "on which device".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("LiveTranslate.AudioSessions")

# A peak below this counts as silence (GetPeakValue is 0.0-1.0).
PEAK_SILENCE = 0.001

# Sentinel for the shared system-sounds session (pid 0), which has no process.
# The UI translates it; keeping the string out of the data layer avoids
# hard-coding a display language here.
SYSTEM_LABEL = "__system__"

# Endpoints whose sessions are never worth reporting (loopback taps and mics
# show up as ordinary endpoints too).
_IGNORED_NAME_HINTS = ("loopback", "立体声混音", "stereo mix")

_NORMALIZE_RE = re.compile(r"[\s()（）\[\]{}]|realtek|r\)|audio", re.IGNORECASE)


@dataclass(frozen=True)
class SessionInfo:
    """One application's audio stream on one endpoint."""

    pid: int
    process: str
    display_name: str = ""
    peak: float = 0.0
    muted: bool = False

    @property
    def is_playing(self) -> bool:
        return self.peak > PEAK_SILENCE

    @property
    def label(self) -> str:
        # pid 0 is the shared system-sounds session: no process, and its display
        # name comes back as an unresolved resource reference.
        if self.pid <= 0:
            return SYSTEM_LABEL
        return self.process or self.display_name or f"pid {self.pid}"


@dataclass(frozen=True)
class EndpointSessions:
    """The sessions rendering to a single output endpoint."""

    name: str
    sessions: list[SessionInfo] = field(default_factory=list)
    error: str = ""

    @property
    def playing(self) -> list[SessionInfo]:
        return [s for s in self.sessions if s.is_playing]


@dataclass(frozen=True)
class Diagnosis:
    """What the session snapshot means for the endpoint we are capturing."""

    level: str  # "ok" | "warn" | "info" | "unknown"
    captured: str = ""
    playing: tuple[str, ...] = ()      # app labels on the captured endpoint
    other_playing: tuple[str, ...] = ()  # app labels on any other endpoint
    endpoint_count: int = 0


def available() -> bool:
    """True when pycaw/comtypes can be used on this machine."""
    try:
        import comtypes  # noqa: F401
        import pycaw  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def _friendly_name(endpoint) -> str:
    return getattr(endpoint, "FriendlyName", "") or ""


def _looks_like_tap(name: str) -> bool:
    low = name.lower()
    return any(hint.lower() in low for hint in _IGNORED_NAME_HINTS)


def _name_key(name: str) -> str:
    """Loose key for matching a PortAudio device name to a pycaw endpoint name.

    PortAudio reports ``扬声器 (Realtek(R) Audio)`` where the endpoint API may
    report ``扬声器 (Realtek Audio)``; comparing stripped-down forms beats
    comparing raw strings.
    """
    return _NORMALIZE_RE.sub("", name or "").strip().lower()


def _process_name(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        import psutil

        return psutil.Process(pid).name()
    except Exception:  # noqa: BLE001
        return ""


def _read_session(ctl) -> SessionInfo | None:
    """Build a SessionInfo from an IAudioSessionControl, or None."""
    from pycaw.pycaw import IAudioMeterInformation
    from pycaw.api.audiopolicy import IAudioSessionControl2

    try:
        ctl2 = ctl.QueryInterface(IAudioSessionControl2)
        pid = ctl2.GetProcessId()
    except Exception:  # noqa: BLE001
        return None

    peak = 0.0
    try:
        peak = float(ctl.QueryInterface(IAudioMeterInformation).GetPeakValue())
    except Exception:  # noqa: BLE001
        pass

    muted = False
    try:
        muted = bool(ctl.QueryInterface(__import__(
            "pycaw.api.endpointvolume", fromlist=["ISimpleAudioVolume"]
        ).ISimpleAudioVolume).GetMute())
    except Exception:  # noqa: BLE001
        pass

    display = ""
    try:
        display = _display_name(ctl2.GetDisplayName())
    except Exception:  # noqa: BLE001
        pass

    return SessionInfo(
        pid=pid, process=_process_name(pid), display_name=display, peak=peak, muted=muted
    )


def _display_name(raw: str) -> str:
    """Clean up IAudioSessionControl2.GetDisplayName().

    Windows returns indirect resource references like
    ``@%SystemRoot%\\System32\\AudioSrv.Dll,-202`` for system sounds rather than
    a human-readable name, so treat those as "no name".
    """
    raw = (raw or "").strip()
    return "" if raw.startswith("@") else raw


def list_by_endpoint() -> list[EndpointSessions]:
    """Render endpoints with their sessions, via the shared cached monitor."""
    return _default_monitor().snapshot()


class SessionMonitor:
    """Enumerates sessions over a **cached** endpoint list.

    Measured on this machine: walking the device tree costs ~2.1s *every call*
    (the OS caches nothing), while re-reading sessions on already-resolved
    endpoints is ~0.2s. So the endpoint list is resolved once and reused, which
    is what makes a periodically-refreshing UI panel affordable.

    Not thread-safe: keep one instance per thread (COM apartments are
    per-thread), which in practice means the Qt main thread.
    """

    def __init__(self):
        self._endpoints: list = []
        self._resolved = False
        self._com_active = False
        self._last_error = ""

    # -- lifecycle ---------------------------------------------------------

    def _ensure_com(self) -> bool:
        if self._com_active:
            return True
        if not available():
            self._last_error = "pycaw/comtypes unavailable"
            return False
        import comtypes

        try:
            comtypes.CoInitialize()
            self._com_active = True
            return True
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            return False

    def invalidate(self) -> None:
        """Drop the cached endpoint list (call on device hotplug)."""
        self._endpoints = []
        self._resolved = False

    def close(self) -> None:
        self.invalidate()
        if self._com_active:
            try:
                import comtypes

                comtypes.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass
            self._com_active = False

    # -- enumeration -------------------------------------------------------

    def _resolve_endpoints(self) -> None:
        """Find render endpoints once. ~2.1s, so it is cached."""
        from pycaw.pycaw import AudioUtilities, IMMEndpoint

        found = []
        try:
            devices = list(AudioUtilities.GetAllDevices())
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            devices = []

        for dev in devices:
            name = _friendly_name(dev)
            if not name or _looks_like_tap(name):
                continue
            if not self._is_render(dev, IMMEndpoint):
                continue
            key = _name_key(name) or name
            if any(_name_key(d[0]) == key for d in found):
                continue  # a device can be registered several times
            found.append((name, dev))

        self._endpoints = found
        self._resolved = True

    @staticmethod
    def _is_render(dev, imm_endpoint_cls) -> bool:
        """0 == eRender. Queried directly: the helper rebuilds a device
        enumerator per call, which is where the seconds go."""
        try:
            return dev._dev.QueryInterface(imm_endpoint_cls).GetDataFlow() == 0
        except Exception:  # noqa: BLE001
            return False

    def snapshot(self) -> list[EndpointSessions]:
        """Sessions per render endpoint. ~0.2s once the list is cached."""
        if not self._ensure_com():
            return []
        if not self._resolved:
            self._resolve_endpoints()

        out: list[EndpointSessions] = []
        for name, dev in self._endpoints:
            try:
                enum = dev.AudioSessionManager.GetSessionEnumerator()
                sessions = []
                for i in range(enum.GetCount()):
                    info = _read_session(enum.GetSession(i))
                    if info is not None:
                        sessions.append(info)
                out.append(EndpointSessions(name=name, sessions=sessions))
            except Exception as exc:  # noqa: BLE001
                # Inactive / unplugged endpoints raise
                # AUDCLNT_E_DEVICE_INVALIDATED. Expected with stale HDMI /
                # Bluetooth endpoints registered.
                out.append(EndpointSessions(name=name, error=str(exc)))
        return out

    def default_endpoint(self) -> str:
        if not self._ensure_com():
            return ""
        from pycaw.pycaw import AudioUtilities

        try:
            return _friendly_name(AudioUtilities.GetSpeakers())
        except Exception:  # noqa: BLE001
            return ""


_DEFAULT_MONITOR: SessionMonitor | None = None


def _default_monitor() -> SessionMonitor:
    global _DEFAULT_MONITOR
    if _DEFAULT_MONITOR is None:
        _DEFAULT_MONITOR = SessionMonitor()
    return _DEFAULT_MONITOR


def default_endpoint_name() -> str:
    """FriendlyName of Windows' default render endpoint."""
    return _default_monitor().default_endpoint()


def list_sessions() -> list[SessionInfo]:
    """Flattened, de-duplicated session list across all endpoints."""
    merged: dict[tuple[int, str], SessionInfo] = {}
    for ep in list_by_endpoint():
        for s in ep.sessions:
            key = (s.pid, s.display_name)
            prev = merged.get(key)
            if prev is None or s.peak > prev.peak:
                merged[key] = s
    return sorted(merged.values(), key=lambda x: (-x.peak, x.process))


def active_endpoints() -> list[str]:
    """Names of endpoints that actually responded (i.e. are usable)."""
    return [ep.name for ep in list_by_endpoint() if not ep.error]


def diagnose(
    captured_device: str | None, endpoints: list[EndpointSessions] | None = None
) -> Diagnosis:
    """Summarise whether the current capture is actually isolated.

    ``captured_device`` is the device name as the capture layer reports it, or
    ``None``/``"__default__"`` for "whatever Windows' default is".

    ``endpoints`` lets a caller that already took a snapshot hand it in; the
    enumeration is cached, but a second pass still costs ~0.25s.
    """
    endpoints = list_by_endpoint() if endpoints is None else endpoints
    if not endpoints:
        return Diagnosis(level="unknown")

    usable = [ep for ep in endpoints if not ep.error]
    captured = ""
    want_default = not captured_device or captured_device in ("__default__", "default")
    if want_default:
        captured = default_endpoint_name()
    key = _name_key(captured_device or "")
    if not captured and key:
        for ep in usable:
            if _name_key(ep.name) == key:
                captured = ep.name
                break
    if not captured and usable:
        # Last resort only -- prefer default_endpoint_name(), which asks Windows
        # rather than assuming the first enumerated endpoint is the default.
        captured = usable[0].name

    on_captured: list[str] = []
    elsewhere: list[str] = []
    for ep in usable:
        names = [s.label for s in ep.playing]
        if ep.name == captured:
            on_captured.extend(names)
        else:
            elsewhere.extend(names)

    on_captured = sorted(set(on_captured))
    elsewhere = sorted(set(elsewhere))

    if not usable:
        level = "unknown"
    elif not on_captured:
        level = "ok"  # nothing is rendering to the endpoint we capture
    elif len(on_captured) == 1:
        # A single source on the captured endpoint means the endpoint mix *is*
        # that source, so isolation is effectively achieved without any routing.
        level = "ok"
    else:
        level = "warn"

    return Diagnosis(
        level=level,
        captured=captured,
        playing=tuple(on_captured),
        other_playing=tuple(elsewhere),
        endpoint_count=len(usable),
    )
