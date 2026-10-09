"""WASAPI **process loopback**: capture a single process's audio by PID.

Why this is here
----------------
Endpoint loopback (what ``audio/capture.py`` uses) captures the whole endpoint
mix, so captioning one player means either routing it to a second output device
(the "B" half of audio-source filtering) or accepting everything on that device.
Process loopback is the only API that filters by process, and it needs no second
device at all.

The API
-------
``ActivateAudioInterfaceAsync`` with
``AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK`` and the magic device name
``VAD\\Process_Loopback``. It is asynchronous: you pass a COM callback object and
Windows calls it back on a COM-owned thread. Available since Windows 10 2004
(build 19041).

The trap that costs an afternoon
--------------------------------
The completion handler **must also implement ``IAgileObject``**. It is a marker
interface with no methods -- purely the promise "you may call this object from
any apartment" -- but WASAPI asks for that IID specifically, and answering
``E_NOINTERFACE`` (i.e. implementing only the completion interface) makes
``ActivateAudioInterfaceAsync`` fail with **``E_ILLEGAL_METHOD_CALL``**, which
gives no hint whatsoever about the cause.

Two dead ends worth not repeating:

* The MSDN note about that error blames a wrong COM apartment or a handler
  "not implemented on an agile object (aggregating a free-threaded marshaler)".
  A hand-rolled free-threaded-marshaler implementation (``IMarshal`` ->
  ``CoCreateFreeThreadedMarshaler``) is *not* enough and does not help.
* The apartment does not matter. Both STA and MTA work once ``IAgileObject`` is
  present, which is why nothing here plays games with ``CoInitializeEx`` or
  ``sys.coinit_flags``.

Format
------
Unlike endpoint loopback there is no need to accept the device mix format and
resample: the client can be initialised with the format we actually want. This
requests **16 kHz mono 16-bit PCM**, which is exactly what ASR consumes, so the
hot path has no conversion step.

Everything here is read-only with respect to what the user hears.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import POINTER, byref, c_uint32, c_uint64, c_void_p, c_wchar_p
from ctypes import wintypes

log = logging.getLogger("LiveTranslate.ProcessLoopback")

VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000

AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE = 1

VT_BLOB = 65

AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x1
AUDCLNT_BUFFERFLAGS_SILENT = 0x2

WAVE_FORMAT_PCM = 0x0001

# hnsBufferDuration is in 100-ns units.
BUFFER_DURATION_100NS = 200 * 10000  # 200 ms

MIN_WINDOWS_BUILD = 19041

# Optional: set a module-level hook to run work on the thread that owns the
# activation. Left in place because callers may need it; nothing sets it today.
_ = None


def windows_build() -> int:
    """Real build number, read from the registry.

    ``sys.getwindowsversion()`` is subject to app-compat shimming, and this
    gates a feature, so read what the OS actually reports.
    """
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        ) as key:
            return int(winreg.QueryValueEx(key, "CurrentBuildNumber")[0])
    except Exception:  # noqa: BLE001
        try:
            return int(ctypes.windll.ntdll.RtlGetVersion.__doc__ or 0)
        except Exception:  # noqa: BLE001
            import sys

            return getattr(sys.getwindowsversion(), "build", 0)


def available() -> bool:
    """True when this OS can do process loopback."""
    try:
        return windows_build() >= MIN_WINDOWS_BUILD
    except Exception:  # noqa: BLE001
        return False


# ── COM interfaces ───────────────────────────────────────────────────────────
# comtypes is used (rather than raw ctypes) because IAudioClient is already
# defined by pycaw, which is a comtypes package.

from comtypes import COMMETHOD, GUID, HRESULT, IUnknown  # noqa: E402


class _PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [
        ("TargetProcessId", wintypes.DWORD),
        ("ProcessLoopbackMode", ctypes.c_int),
    ]


class _ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ActivationType", ctypes.c_int),
        ("ProcessLoopbackParams", _PROCESS_LOOPBACK_PARAMS),
    ]


class _PROPVARIANT(ctypes.Structure):
    """Just enough of PROPVARIANT to carry a VT_BLOB.

    vt + 3 reserved USHORTs is 8 bytes, which is where the union starts on both
    x86 and x64.
    """

    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("wReserved1", ctypes.c_ushort),
        ("wReserved2", ctypes.c_ushort),
        ("wReserved3", ctypes.c_ushort),
        ("blob_cbSize", ctypes.c_ulong),
        ("blob_pBlobData", c_void_p),
    ]


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = (
        COMMETHOD(
            [],
            HRESULT,
            "GetActivateResult",
            (["out"], POINTER(HRESULT), "activateResult"),
            (["out"], POINTER(POINTER(IUnknown)), "activatedInterface"),
        ),
    )


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = (
        COMMETHOD(
            [],
            HRESULT,
            "ActivateCompleted",
            (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "activateOperation"),
        ),
    )


class IAgileObject(IUnknown):
    """Marker interface: no methods.

    Declaring it is what makes the handler acceptable to
    ``ActivateAudioInterfaceAsync``. Without it the call fails with
    ``E_ILLEGAL_METHOD_CALL`` and no explanation.
    """

    _iid_ = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
    _methods_ = ()


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
    _methods_ = (
        COMMETHOD(
            [],
            HRESULT,
            "GetBuffer",
            (["out"], POINTER(POINTER(ctypes.c_byte)), "ppData"),
            (["out"], POINTER(c_uint32), "pNumFramesToRead"),
            (["out"], POINTER(c_uint32), "pdwFlags"),
            (["out"], POINTER(c_uint64), "pu64DevicePosition"),
            (["out"], POINTER(c_uint64), "pu64QPCPosition"),
        ),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "NumFramesRead")),
        COMMETHOD(
            [],
            HRESULT,
            "GetNextPacketSize",
            (["out"], POINTER(c_uint32), "pNumFramesInNextPacket"),
        ),
    )


class ProcessLoopbackError(RuntimeError):
    """Raised when activation or stream setup fails."""


# ── activation ───────────────────────────────────────────────────────────────

_ACTIVATE_ARGS_READY = False


def _activate_fn():
    global _ACTIVATE_ARGS_READY
    fn = ctypes.windll.mmdevapi.ActivateAudioInterfaceAsync
    if not _ACTIVATE_ARGS_READY:
        fn.restype = HRESULT
        fn.argtypes = [
            c_wchar_p,
            POINTER(GUID),
            POINTER(_PROPVARIANT),
            POINTER(IActivateAudioInterfaceCompletionHandler),
            POINTER(POINTER(IActivateAudioInterfaceAsyncOperation)),
        ]
        _ACTIVATE_ARGS_READY = True
    return fn


class _CompletionHandler(object):
    """Holds the COM object plus the state the waiting thread needs."""

    def __init__(self):
        import comtypes

        handler = self

        class _Handler(comtypes.COMObject):
            # IAgileObject is mandatory; see the module docstring.
            _com_interfaces_ = [
                IActivateAudioInterfaceCompletionHandler,
                IAgileObject,
            ]

            def ActivateCompleted(self, activateOperation):  # noqa: N802
                try:
                    handler.operation = activateOperation
                except Exception as exc:  # noqa: BLE001
                    handler.error = exc
                finally:
                    handler.done.set()
                return 0  # S_OK

        self.done = threading.Event()
        self.operation = None
        self.error = None
        self._obj = _Handler()


def _make_propvariant(pid: int, include_tree: bool) -> _PROPVARIANT:
    mode = (
        PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
        if include_tree
        else PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE
    )
    params = _ACTIVATION_PARAMS(
        ActivationType=AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
        ProcessLoopbackParams=_PROCESS_LOOPBACK_PARAMS(
            TargetProcessId=pid, ProcessLoopbackMode=mode
        ),
    )
    pv = _PROPVARIANT()
    pv.vt = VT_BLOB
    pv.blob_cbSize = ctypes.sizeof(params)
    pv.blob_pBlobData = ctypes.cast(byref(params), c_void_p)
    # Keep the params alive: the async activation reads the blob after the call
    # returns.
    pv._params = params  # type: ignore[attr-defined]
    return pv


def activate_process_loopback(pid: int, include_tree: bool = True, timeout: float = 5.0):
    """Activate an IAudioClient scoped to ``pid``'s audio.

    ``include_tree=True`` captures the target process and its children;
    ``False`` captures everything *except* that tree, which is how a caller
    avoids capturing its own playback.
    """
    if not available():
        raise ProcessLoopbackError(
            f"process loopback needs Windows build >= {MIN_WINDOWS_BUILD} "
            f"(this is {windows_build()})"
        )

    from pycaw.api.audioclient import IAudioClient

    pv = _make_propvariant(pid, include_tree)
    handler = _CompletionHandler()
    interface = handler._obj.QueryInterface(IActivateAudioInterfaceCompletionHandler)

    fn = _activate_fn()
    op = POINTER(IActivateAudioInterfaceAsyncOperation)()
    hr = fn(
        VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
        byref(IAudioClient._iid_),
        byref(pv),
        interface,
        byref(op),
    )
    if hr != 0:
        raise ProcessLoopbackError(
            f"ActivateAudioInterfaceAsync failed: 0x{hr & 0xFFFFFFFF:08X}"
        )

    if not handler.done.wait(timeout):
        raise ProcessLoopbackError("activation callback never fired")
    if handler.error is not None:
        raise ProcessLoopbackError(f"completion handler error: {handler.error}")

    operation = handler.operation or (op if op else None)
    if not operation:
        raise ProcessLoopbackError("no activation operation returned")

    act_hr, iface = operation.GetActivateResult()
    if act_hr != 0:
        raise ProcessLoopbackError(
            f"GetActivateResult failed: 0x{act_hr & 0xFFFFFFFF:08X}"
        )
    if not iface:
        raise ProcessLoopbackError("activation returned no interface")

    client = iface.QueryInterface(IAudioClient)
    # Keep the handler object alive for as long as the client lives; Windows
    # holds a reference until the operation completes, and dropping the Python
    # wrapper early can free the vtable out from under it.
    client._lt_handler = handler  # type: ignore[attr-defined]
    return client


# ── capture ──────────────────────────────────────────────────────────────────

class ProcessLoopbackCapture:
    """Captures one process's audio at 16 kHz mono 16-bit PCM.

    Context manager; ``read()`` returns whatever is currently buffered, so it
    never blocks waiting for audio that is not there.
    """

    SAMPLE_RATE = 16000
    CHANNELS = 1
    BITS = 16

    def __init__(self, pid: int, include_tree: bool = True):
        self.pid = pid
        self.include_tree = include_tree
        self.client = None
        self.capture = None
        self.format = None
        self._block_align = self.CHANNELS * self.BITS // 8
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        from pycaw.api.audioclient.depend import WAVEFORMATEX

        self.client = activate_process_loopback(self.pid, self.include_tree)

        fmt = WAVEFORMATEX()
        fmt.wFormatTag = WAVE_FORMAT_PCM
        fmt.nChannels = self.CHANNELS
        fmt.nSamplesPerSec = self.SAMPLE_RATE
        fmt.wBitsPerSample = self.BITS
        fmt.nBlockAlign = self.CHANNELS * self.BITS // 8
        fmt.nAvgBytesPerSec = self.SAMPLE_RATE * fmt.nBlockAlign
        fmt.cbSize = 0
        self._block_align = fmt.nBlockAlign

        # Unlike endpoint loopback, the client can be initialised with the format
        # we want rather than the device mix format -- so no resampling here.
        self.client.Initialize(
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK,
            BUFFER_DURATION_100NS,
            0,
            byref(fmt),
            None,
        )
        self.format = fmt

        self.capture = self.client.GetService(
            IAudioCaptureClient._iid_
        ).QueryInterface(IAudioCaptureClient)
        self.client.Start()
        self._started = True
        log.info(
            f"Process loopback capturing pid={self.pid} "
            f"({self.SAMPLE_RATE}Hz {self.CHANNELS}ch {self.BITS}bit, "
            f"{'tree' if self.include_tree else 'excluding tree'})"
        )

    def close(self) -> None:
        if self.client is not None and self._started:
            try:
                self.client.Stop()
            except Exception:  # noqa: BLE001
                pass
        self._started = False
        self.capture = None
        self.client = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- reading -----------------------------------------------------------

    def read(self) -> bytes:
        """Drain the buffered packets and return the raw PCM as one buffer.

        Empty bytes means nothing was captured in this window (silence, or the
        target is not rendering anything). Callers should not treat that as an
        error -- a process that is paused produces no packets at all.

        Note the call shapes: comtypes treats ``[out]`` parameters as return
        values, so these methods take no arguments and hand back the outputs.
        """
        if self.capture is None:
            raise ProcessLoopbackError("capture is not open")

        chunks: list[bytes] = []
        while True:
            try:
                next_size = self.capture.GetNextPacketSize()
            except Exception as exc:  # noqa: BLE001
                log.debug(f"GetNextPacketSize failed: {exc}")
                break
            if not next_size:
                break

            try:
                data_ptr, frames, flags = self.capture.GetBuffer()[:3]
            except Exception as exc:  # noqa: BLE001
                log.debug(f"GetBuffer failed: {exc}")
                break
            try:
                if frames and data_ptr:
                    nbytes = frames * self._block_align
                    if flags & AUDCLNT_BUFFERFLAGS_SILENT:
                        chunks.append(b"\x00" * nbytes)
                    else:
                        chunks.append(ctypes.string_at(data_ptr, nbytes))
            finally:
                try:
                    self.capture.ReleaseBuffer(frames)
                except Exception as exc:  # noqa: BLE001
                    log.debug(f"ReleaseBuffer failed: {exc}")

        return b"".join(chunks)

    def read_for(self, seconds: float, poll: float = 0.01) -> bytes:
        """Collect everything captured over ``seconds`` (blocking)."""
        deadline = time.perf_counter() + seconds
        out: list[bytes] = []
        while time.perf_counter() < deadline:
            data = self.read()
            if data:
                out.append(data)
            else:
                time.sleep(poll)
        return b"".join(out)


def find_pids_by_name(name: str) -> list[int]:
    """PIDs whose executable matches ``name`` (case-insensitive, with or
    without the .exe suffix). Empty when psutil cannot see them."""
    wanted = name.lower()
    if not wanted.endswith(".exe"):
        wanted += ".exe"
    found: list[int] = []
    try:
        import psutil

        for proc in psutil.process_iter(["name"]):
            try:
                if (proc.info.get("name") or "").lower() == wanted:
                    found.append(proc.pid)
            except Exception:  # noqa: BLE001
                continue
    except Exception as exc:  # noqa: BLE001
        log.warning(f"process lookup failed: {exc}")
    return found
