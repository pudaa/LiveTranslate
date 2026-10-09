"""Probe the COM apartment model around ActivateAudioInterfaceAsync.

E_ILLEGAL_METHOD_CALL (0x8000000E) from ActivateAudioInterfaceAsync usually means
the calling apartment is wrong, so this reports the apartment before and after
comtypes is imported, and tries the activation from a dedicated MTA thread.
"""

import ctypes
import sys
import threading
from ctypes import POINTER, byref, c_uint, c_void_p, c_wchar_p
from ctypes import wintypes

ole32 = ctypes.windll.ole32

APTTYPES = {0: "STA", 1: "MTA", 2: "NA", 3: "MAIN_STA", 4: "IMPLICIT_MTA"}


def apartment() -> str:
    t, q = c_uint(), c_uint()
    hr = ole32.CoGetApartmentType(byref(t), byref(q))
    if hr != 0:
        return f"<hr=0x{hr & 0xFFFFFFFF:08X}>"
    return f"{APTTYPES.get(t.value, t.value)} (qualifier={q.value})"


def report(label: str) -> None:
    print(f"  {label:34} {apartment()}")


print("=== apartment model ===")
report("before importing comtypes")
import comtypes  # noqa: E402

report("after  importing comtypes")
try:
    comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    print("  CoInitializeEx(MULTITHREADED)        ok")
except OSError as exc:
    print(f"  CoInitializeEx(MULTITHREADED)        failed {exc.winerror}")
report("after  CoInitializeEx(MTA)")

from comtypes import COMMETHOD, GUID, HRESULT, IUnknown  # noqa: E402
from pycaw.api.audioclient import IAudioClient  # noqa: E402

VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
VT_BLOB = 65


class PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [("TargetProcessId", wintypes.DWORD), ("ProcessLoopbackMode", ctypes.c_int)]


class ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ActivationType", ctypes.c_int),
        ("ProcessLoopbackParams", PROCESS_LOOPBACK_PARAMS),
    ]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("r1", ctypes.c_ushort),
        ("r2", ctypes.c_ushort),
        ("r3", ctypes.c_ushort),
        ("cbSize", ctypes.c_ulong),
        ("pBlobData", c_void_p),
    ]


class IOp(IUnknown):
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


class IHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = (
        COMMETHOD(
            [],
            HRESULT,
            "ActivateCompleted",
            (["in"], POINTER(IOp), "activateOperation"),
        ),
    )


def make_pv(pid: int):
    params = ACTIVATION_PARAMS(
        ActivationType=AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
        ProcessLoopbackParams=PROCESS_LOOPBACK_PARAMS(TargetProcessId=pid, ProcessLoopbackMode=0),
    )
    pv = PROPVARIANT()
    pv.vt = VT_BLOB
    pv.cbSize = ctypes.sizeof(params)
    pv.pBlobData = ctypes.cast(byref(params), c_void_p)
    pv._keep = params
    return pv


def try_activate(pid: int, label: str) -> None:
    fn = ctypes.windll.mmdevapi.ActivateAudioInterfaceAsync
    fn.restype = HRESULT
    fn.argtypes = [
        c_wchar_p,
        POINTER(GUID),
        POINTER(PROPVARIANT),
        POINTER(IHandler),
        POINTER(POINTER(IOp)),
    ]
    pv = make_pv(pid)

    class _H(comtypes.COMObject):
        _com_interfaces_ = [IHandler]

        def ActivateCompleted(self, op):
            return 0

    h = _H()
    op = POINTER(IOp)()
    try:
        hr = fn(
            VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
            byref(IAudioClient._iid_),
            byref(pv),
            h.QueryInterface(IHandler),
            byref(op),
        )
        print(f"  {label:34} hr=0x{hr & 0xFFFFFFFF:08X}  {'OK' if hr == 0 else 'FAIL'}")
    except OSError as exc:
        print(f"  {label:34} OSError {exc.winerror} (0x{exc.winerror & 0xFFFFFFFF:08X})")


print("\n=== activate (any pid works; it only needs to be a valid call) ===")
pid = ctypes.windll.kernel32.GetCurrentProcessId()
print(f"  using pid={pid} (this process)")
try_activate(pid, "on the current (main) thread")

result = {}


def worker():
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError:
        pass
    result["apt"] = apartment()
    try_activate(pid, "on a fresh MTA thread")


print("\n=== same call from a dedicated MTA thread ===")
t = threading.Thread(target=worker)
t.start()
t.join()
print(f"  (that thread's apartment was {result.get('apt')})")
