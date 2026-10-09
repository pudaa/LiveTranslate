"""Proof of concept: WASAPI **process loopback** -- capture one process's audio by PID.

Why this matters
----------------
WASAPI endpoint loopback (what the app uses today) captures the *whole endpoint
mix*, so captioning one player means either routing it to a second output device
(the "B" half) or accepting everything on that device. Process loopback is the
only API that filters by process, and it needs no second device.

Why it has to be hand-rolled
----------------------------
Nothing on PyPI wraps it. ``pycaw`` provides ``IAudioClient`` and
``WAVEFORMATEX`` but not ``IAudioCaptureClient``, the async-activation
interfaces, or the activation parameter structs -- so those are defined here with
comtypes.

The awkward part is that ``ActivateAudioInterfaceAsync`` is *asynchronous*: you
hand it a COM callback object and it calls back on a COM thread, which means
implementing a COM interface (``IActivateAudioInterfaceCompletionHandler``) in
Python and bridging it to a ``threading.Event``.

This script is deliberately standalone and self-testing: it spawns a child that
actually plays a tone, captures that child's PID, and asserts the audio is
non-silent -- then captures an unrelated PID and asserts it is silent. Non-silent
alone would only prove "something was captured"; both halves together prove
*isolation*, which is the whole point.

Run with the project env python:
    python doc/migrations/poc_process_loopback.py
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import threading
import time
import wave
from ctypes import POINTER, byref, c_uint32, c_uint64, c_void_p, c_wchar_p
from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import comtypes  # noqa: E402
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown  # noqa: E402
from pycaw.api.audioclient import IAudioClient  # noqa: E402
from pycaw.api.audioclient.depend import WAVEFORMATEX  # noqa: E402

# ── constants (Windows SDK: audioclient.h, mmdeviceapi.h) ────────────────────

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
WAVE_FORMAT_IEEE_FLOAT = 0x0003

# hnsBufferDuration is in 100-ns units; 20 ms matches the MS sample.
BUFFER_DURATION_100NS = 20 * 10000


# ── activation parameter structs ─────────────────────────────────────────────

class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [
        ("TargetProcessId", wintypes.DWORD),
        ("ProcessLoopbackMode", ctypes.c_int),
    ]


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ActivationType", ctypes.c_int),
        ("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS),
    ]


class PROPVARIANT(ctypes.Structure):
    """Just enough of PROPVARIANT to carry a VT_BLOB.

    Layout matters: vt + 3 reserved USHORTs is 8 bytes, which is where the union
    starts on both x86 and x64.
    """

    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("wReserved1", ctypes.c_ushort),
        ("wReserved2", ctypes.c_ushort),
        ("wReserved3", ctypes.c_ushort),
        ("blob_cbSize", ctypes.c_ulong),
        ("blob_pBlobData", c_void_p),
    ]


# ── the three interfaces pycaw does not provide ──────────────────────────────

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


class _ActivationHandler(comtypes.COMObject):
    """Bridges the COM callback back to a threading.Event.

    ActivateCompleted fires on a COM-owned thread, so the waiting thread must not
    touch the operation directly -- it waits on the event and then reads
    ``operation``, which is safe because the event set happens-after the store.
    """

    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler]

    def __init__(self):
        super().__init__()
        self.done = threading.Event()
        self.operation = None
        self.error = None

    def ActivateCompleted(self, activateOperation):
        try:
            if activateOperation:
                self.operation = activateOperation.QueryInterface(
                    IActivateAudioInterfaceAsyncOperation
                )
        except Exception as exc:  # noqa: BLE001
            self.error = exc
        finally:
            self.done.set()
        return 0  # S_OK


# ── the native entry point ───────────────────────────────────────────────────

def _load_activate():
    fn = ctypes.windll.mmdevapi.ActivateAudioInterfaceAsync
    fn.restype = HRESULT
    fn.argtypes = [
        c_wchar_p,
        POINTER(GUID),
        POINTER(PROPVARIANT),
        POINTER(IActivateAudioInterfaceCompletionHandler),
        POINTER(POINTER(IActivateAudioInterfaceAsyncOperation)),
    ]
    return fn


def _make_params(pid: int, include_tree: bool) -> PROPVARIANT:
    mode = (
        PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
        if include_tree
        else PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE
    )
    params = AUDIOCLIENT_ACTIVATION_PARAMS(
        ActivationType=AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
        ProcessLoopbackParams=AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(
            TargetProcessId=pid, ProcessLoopbackMode=mode
        ),
    )
    pv = PROPVARIANT()
    pv.vt = VT_BLOB
    pv.blob_cbSize = ctypes.sizeof(params)
    # Keep the buffer alive on the PROPVARIANT so it is not collected while the
    # async activation is still reading it.
    pv._params = params  # type: ignore[attr-defined]
    pv.blob_pBlobData = ctypes.cast(byref(params), c_void_p)
    return pv


def _ensure_com() -> None:
    """ActivateAudioInterfaceAsync needs COM initialised on the calling thread.

    Without it the call fails with E_ILLEGAL_METHOD_CALL (0x8000000E), which is
    a confusing error for "you forgot CoInitializeEx". The completion callback is
    dispatched on a COM-owned thread, so MTA is the right model here.

    RPC_E_CHANGED_MODE (0x80010106) means the thread was already initialised with
    the other apartment model -- fine, just leave it alone.
    """
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError as exc:
        if exc.winerror != -2147417850:  # RPC_E_CHANGED_MODE
            raise


def activate_process_loopback(pid: int, include_tree: bool = True, timeout: float = 5.0):
    """Return an IAudioClient bound to process loopback for ``pid``."""
    _ensure_com()
    fn = _load_activate()
    pv = _make_params(pid, include_tree)
    handler = _ActivationHandler()
    op = POINTER(IActivateAudioInterfaceAsyncOperation)()

    hr = fn(
        VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
        byref(IAudioClient._iid_),
        byref(pv),
        handler.QueryInterface(IActivateAudioInterfaceCompletionHandler),
        byref(op),
    )
    if hr != 0:
        raise OSError(f"ActivateAudioInterfaceAsync failed: 0x{hr & 0xFFFFFFFF:08X}")

    if not handler.done.wait(timeout):
        raise TimeoutError("activation callback never fired")
    if handler.error is not None:
        raise RuntimeError(f"completion handler error: {handler.error}")

    operation = handler.operation or (op if op else None)
    if not operation:
        raise RuntimeError("no activation operation returned")

    act_hr, iface = operation.GetActivateResult()
    if act_hr != 0:
        raise OSError(f"GetActivateResult failed: 0x{act_hr & 0xFFFFFFFF:08X}")
    if not iface:
        raise RuntimeError("activation returned no interface")
    return iface.QueryInterface(IAudioClient)


def _make_format(channels: int, rate: int, bits: int, float_pcm: bool) -> WAVEFORMATEX:
    fmt = WAVEFORMATEX()
    fmt.wFormatTag = WAVE_FORMAT_IEEE_FLOAT if float_pcm else WAVE_FORMAT_PCM
    fmt.nChannels = channels
    fmt.nSamplesPerSec = rate
    fmt.wBitsPerSample = bits
    fmt.nBlockAlign = channels * bits // 8
    fmt.nAvgBytesPerSec = rate * fmt.nBlockAlign
    fmt.cbSize = 0
    return fmt


class ProcessLoopbackCapture:
    """Captures one PID's audio. Context manager; yields raw PCM packets."""

    # Process loopback is picky about the format, and which one it accepts varies
    # with the render device in use, so try the common ones in order.
    CANDIDATE_FORMATS = (
        (2, 48000, 32, True),   # float32 stereo 48k -- most common mix format
        (2, 48000, 16, False),
        (2, 44100, 16, False),  # what the MS ApplicationLoopback sample uses
    )

    def __init__(self, pid: int, include_tree: bool = True):
        self.pid = pid
        self.include_tree = include_tree
        self.client = None
        self.capture = None
        self.fmt = None
        self.channels = 0
        self.rate = 0
        self.bits = 0
        self.is_float = False
        self._last_hr = {}

    def open(self) -> None:
        self.client = activate_process_loopback(self.pid, self.include_tree)

        last_err = None
        for channels, rate, bits, is_float in self.CANDIDATE_FORMATS:
            fmt = _make_format(channels, rate, bits, is_float)
            try:
                self.client.Initialize(
                    AUDCLNT_SHAREMODE_SHARED,
                    AUDCLNT_STREAMFLAGS_LOOPBACK,
                    BUFFER_DURATION_100NS,
                    0,
                    byref(fmt),
                    None,
                )
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                self._last_hr[f"{channels}ch/{rate}/{bits}"] = exc
                continue
            self.fmt = fmt
            self.channels, self.rate, self.bits = channels, rate, bits
            self.is_float = is_float
            break

        if self.fmt is None:
            raise RuntimeError(
                f"no candidate format accepted for pid {self.pid}: {self._last_hr}"
            )

        self.capture = self.client.GetService(IAudioCaptureClient._iid_).QueryInterface(
            IAudioCaptureClient
        )
        self.client.Start()

    def read_frames(self, seconds: float) -> "list[bytes]":
        """Collect raw packets for ``seconds``."""
        import numpy as np

        packets: list[bytes] = []
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            nxt = c_uint32()
            if self.capture.GetNextPacketSize(byref(nxt)) != 0 or nxt.value == 0:
                time.sleep(0.005)
                continue
            data_ptr = POINTER(ctypes.c_byte)()
            frames = c_uint32()
            flags = c_uint32()
            devpos = c_uint64()
            qpc = c_uint64()
            if self.capture.GetBuffer(
                byref(data_ptr), byref(frames), byref(flags), byref(devpos), byref(qpc)
            ) != 0:
                time.sleep(0.005)
                continue
            try:
                if frames.value and data_ptr:
                    nbytes = frames.value * self.fmt.nBlockAlign
                    raw = ctypes.string_at(data_ptr, nbytes)
                    if flags.value & AUDCLNT_BUFFERFLAGS_SILENT:
                        packets.append(b"\x00" * nbytes)
                    else:
                        packets.append(raw)
            finally:
                self.capture.ReleaseBuffer(frames.value)
        return packets

    def close(self) -> None:
        for obj, name in ((self.client, "client"), (self.capture, "capture")):
            if obj is None:
                continue
            try:
                if name == "client":
                    obj.Stop()
            except Exception:  # noqa: BLE001
                pass
        self.client = None
        self.capture = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# ── self-test ────────────────────────────────────────────────────────────────

_TONE_SRC = r"""
import sys, time, winsound
wav, secs = sys.argv[1], float(sys.argv[2])
winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
print("playing", flush=True)
time.sleep(secs)
"""


def _make_tone_wav(path: str, freq: int = 440, seconds: int = 3, rate: int = 44100) -> None:
    import math
    import struct

    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        frames = bytearray()
        for i in range(rate * seconds):
            # Deliberately quiet (~12% amplitude): the test only needs a
            # non-silent signal, and it does not need to be loud in the room.
            v = int(4000 * math.sin(2 * math.pi * freq * i / rate))
            frames += struct.pack("<h", v)
        wf.writeframes(bytes(frames))


def _rms(packets: list[bytes], fmt_bits: int, is_float: bool) -> float:
    import numpy as np

    if not packets:
        return -1.0
    buf = b"".join(packets)
    if not buf:
        return -1.0
    if is_float:
        data = np.frombuffer(buf, dtype=np.float32)
        return float(np.sqrt(np.mean(data**2))) if data.size else -1.0
    if fmt_bits == 16:
        data = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
    else:
        data = np.frombuffer(buf, dtype=np.int32).astype(np.float32) / 2147483648.0
    return float(np.sqrt(np.mean(data**2))) if data.size else -1.0


def main() -> int:
    print(f"python {sys.version.split()[0]}")
    v = sys.getwindowsversion()
    print(f"windows build {v.build}  (process loopback needs >= 19041)")
    if v.build < 19041:
        print("SKIP: build too old for process loopback")
        return 1

    tmp = tempfile.mkdtemp(prefix="lt-plb-")
    wav = os.path.join(tmp, "tone.wav")
    _make_tone_wav(wav)
    print(f"tone: {wav}")

    py = sys.executable
    print("\n[1/3] spawning a child that plays the tone (the capture target)")
    target = subprocess.Popen(
        [py, "-c", _TONE_SRC, wav, "14"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    silent = subprocess.Popen(
        [py, "-c", "import time; time.sleep(14)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        line = target.stdout.readline().strip() if target.stdout else ""
        print(f"  target pid={target.pid}  ({line})")
        print(f"  silent pid={silent.pid}")
        time.sleep(1.2)  # let the audio session come up

        print("\n[2/3] capturing the TARGET pid -- expect NON-silent")
        with ProcessLoopbackCapture(target.pid) as cap:
            print(
                f"  negotiated format: {cap.channels}ch {cap.rate}Hz "
                f"{cap.bits}bit {'float' if cap.is_float else 'pcm'}"
            )
            pkts = cap.read_frames(2.0)
            rms_target = _rms(pkts, cap.bits, cap.is_float)
        print(f"  packets={len(pkts)}  rms={rms_target:.5f}")

        print("\n[3/3] capturing an UNRELATED pid -- expect SILENT (isolation)")
        with ProcessLoopbackCapture(silent.pid) as cap2:
            pkts2 = cap2.read_frames(2.0)
            rms_silent = _rms(pkts2, cap2.bits, cap2.is_float)
        print(f"  packets={len(pkts2)}  rms={rms_silent:.5f}")

        print("\n=== RESULT ===")
        ok_target = rms_target > 0.001
        ok_isolated = rms_silent < 0.001 or rms_silent < 0
        print(f"  target captured audio : {'PASS' if ok_target else 'FAIL'}")
        print(f"  isolation held        : {'PASS' if ok_isolated else 'FAIL'}")
        if ok_target and ok_isolated:
            print("\nPROCESS LOOPBACK WORKS: captured one process, excluded the other.")
            return 0
        print("\nINCONCLUSIVE/FAIL -- see numbers above")
        return 1
    finally:
        for p in (target, silent):
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    sys.exit(main())
