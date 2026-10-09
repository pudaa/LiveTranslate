"""Minimal test: an FTM-agile completion handler makes ActivateAudioInterfaceAsync accept us.

Root cause of the earlier E_ILLEGAL_METHOD_CALL, per the ActivateAudioInterfaceAsync
docs:

    E_ILLEGAL_METHOD_CALL -- On versions of Windows previous to Windows 10, this
    error may result if the function is called from an incorrect COM apartment,
    or **if the passed IActivateAudioInterfaceCompletionHandler is not implemented
    on an agile object (aggregating a free-threaded marshaler)**.

comtypes' COMObject does not aggregate the FTM, so the handler is rejected before
anything else is even considered -- which is why a real render endpoint with NULL
params failed the same way as the process-loopback path.

This builds the handler by hand with ctypes: a vtable of stdcall thunks, a
refcount, and a QueryInterface that hands out the free-threaded marshaler from
CoCreateFreeThreadedMarshaler for IID_IMarshal.

Run: python doc/migrations/probe_agile_handler.py
"""

from __future__ import annotations

import ctypes
import sys
import threading
import uuid
from ctypes import POINTER, byref, c_void_p, c_wchar_p
from ctypes import wintypes

# comtypes pins the importing thread to STA, and then refuses to import at all if
# the thread is already MTA. This probe deliberately owns its own apartment, so
# it must not import comtypes/pycaw -- everything below is hand-rolled. The flag
# is set anyway as a safety net in case something pulls comtypes in transitively.
sys.coinit_flags = 0  # COINIT_MULTITHREADED

ole32 = ctypes.windll.ole32
ole32.CoInitializeEx(None, 0x0)  # must precede any COM use
ole32.CoCreateFreeThreadedMarshaler.argtypes = [c_void_p, POINTER(c_void_p)]
ole32.CoCreateFreeThreadedMarshaler.restype = ctypes.HRESULT

HRESULT = ctypes.c_long


class GUID(ctypes.Structure):
    """Same layout as the Windows GUID; built from the standard string form."""

    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, text: str = ""):
        if not text:
            return  # zeroed; needed so ctypes can build one for pointer access
        u = uuid.UUID(text.strip("{}"))
        self.Data1 = u.time_low
        self.Data2 = u.time_mid
        self.Data3 = u.time_hi_version
        for i, b in enumerate(u.bytes[8:]):
            self.Data4[i] = b


IID_IUNKNOWN = GUID("{00000000-0000-0000-C000-000000000046}")
IID_IMARSHAL = GUID("{00000003-0000-0000-C000-000000000046}")
IID_COMPLETION = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
IID_ASYNC_OP = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
IID_IAUDIO_CLIENT = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")

# The actual blocker. IAgileObject is a marker interface with NO methods: it is
# purely the promise "you may call this object from any apartment". Implementing
# the free-threaded marshaler (IMarshal) is NOT enough -- Windows asks for this
# IID specifically, and answering E_NOINTERFACE makes the activation fail with
# E_ILLEGAL_METHOD_CALL, which gives no hint as to why.
IID_IAGILE_OBJECT = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")

# The default render device can be named directly, so the control case needs no
# device enumeration (and therefore no comtypes).
DEVINTERFACE_AUDIO_RENDER = "{E6327CAD-DCEC-4949-AE8A-991E976A79D2}"

VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
VT_BLOB = 65

# COM is stdcall on Windows.
_QI = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p))
_ADDREF = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)
_RELEASE = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)
_COMPLETED = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_void_p)


class _Vtbl(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", _QI),
        ("AddRef", _ADDREF),
        ("Release", _RELEASE),
        ("ActivateCompleted", _COMPLETED),
    ]


class _Obj(ctypes.Structure):
    _fields_ = [
        ("vtbl", POINTER(_Vtbl)),
        ("refcount", ctypes.c_ulong),
        ("ftm", c_void_p),
        ("done", c_void_p),
        ("operation", c_void_p),
        ("python_self", c_void_p),
    ]


# ctypes callbacks must be kept alive for as long as the native object references
# them, and we need to map a raw `this` pointer back to the Python object.
_OBJECTS: dict[int, "AgileHandler"] = {}
_CALLBACKS: list = []


def _guid_bytes(guid) -> bytes:
    return ctypes.string_at(ctypes.addressof(guid), 16)


def _same_iid(riid_ptr, expected: GUID) -> bool:
    """Compare a REFIID against a GUID by raw bytes.

    ctypes ``Structure`` does not compare by value, so ``left.contents == right``
    is always False -- which made every QueryInterface answer E_NOINTERFACE, and
    that in turn is what made Windows reject the handler as non-agile.
    """
    if not riid_ptr:
        return False
    try:
        return _guid_bytes(riid_ptr.contents) == _guid_bytes(expected)
    except Exception:  # noqa: BLE001
        return False


class AgileHandler:
    """IActivateAudioInterfaceCompletionHandler implemented as an FTM-agile object."""

    def __init__(self):
        self.done = threading.Event()
        self.operation = None
        self.error = None
        self._addr = None
        self._build()

    def _build(self):
        self._obj = _Obj()
        self._obj.refcount = 1
        self._obj.ftm = None
        self._obj.done = None  # set below (needs byref to a stable location)
        self._obj.operation = None
        self._obj.python_self = None

        handlers = {
            "qi": self._qi,
            "addref": self._addref,
            "release": self._release,
            "completed": self._completed,
        }
        cbs = {
            "qi": _QI(handlers["qi"]),
            "addref": _ADDREF(handlers["addref"]),
            "release": _RELEASE(handlers["release"]),
            "completed": _COMPLETED(handlers["completed"]),
        }
        _CALLBACKS.extend(cbs.values())

        self._vtbl = _Vtbl(cbs["qi"], cbs["addref"], cbs["release"], cbs["completed"])
        _CALLBACKS.append(self._vtbl)
        self._obj.vtbl = ctypes.pointer(self._vtbl)

        self._addr = ctypes.addressof(self._obj)
        _OBJECTS[self._addr] = self

        # Create the FTM with ourselves as the outer unknown, so IMarshal calls
        # are delegated back here. Must happen before the object is handed out.
        ftm = c_void_p()
        hr = ole32.CoCreateFreeThreadedMarshaler(c_void_p(self._addr), byref(ftm))
        if hr != 0:
            raise OSError(f"CoCreateFreeThreadedMarshaler failed 0x{hr & 0xFFFFFFFF:08X}")
        self._obj.ftm = ftm

    @property
    def pointer(self):
        return c_void_p(self._addr)

    # -- IUnknown ----------------------------------------------------------

    def _qi(self, this, riid, ppv):
        obj = _OBJECTS.get(this)
        if obj is None or not ppv:
            return -2147467262  # E_NOINTERFACE
        if (
            _same_iid(riid, IID_IUNKNOWN)
            or _same_iid(riid, IID_COMPLETION)
            or _same_iid(riid, IID_IAGILE_OBJECT)
        ):
            obj._obj.refcount += 1
            ppv[0] = this
            return 0
        if _same_iid(riid, IID_IMARSHAL):
            # Hand out the FTM so the handler is usable from any apartment.
            if not obj._obj.ftm:
                return -2147467262
            vtable = ctypes.cast(obj._obj.ftm, POINTER(POINTER(c_void_p)))[0]
            qi = _QI(vtable[0])
            return qi(obj._obj.ftm, riid, ppv)
        ppv[0] = None
        return -2147467262  # E_NOINTERFACE

    def _addref(self, this):
        obj = _OBJECTS.get(this)
        if obj is None:
            return 0
        obj._obj.refcount += 1
        return obj._obj.refcount

    def _release(self, this):
        obj = _OBJECTS.get(this)
        if obj is None:
            return 0
        obj._obj.refcount -= 1
        return obj._obj.refcount

    # -- IActivateAudioInterfaceCompletionHandler ---------------------------

    def _completed(self, this, operation):
        obj = _OBJECTS.get(this)
        if obj is not None:
            try:
                op = ctypes.cast(operation, POINTER(c_void_p))
                obj.operation = ctypes.cast(operation, c_void_p)
            except Exception as exc:  # noqa: BLE001
                obj.error = exc
            finally:
                obj.done.set()
        return 0


class _PLP(ctypes.Structure):
    _fields_ = [("TargetProcessId", wintypes.DWORD), ("ProcessLoopbackMode", ctypes.c_int)]


class _AP(ctypes.Structure):
    _fields_ = [("ActivationType", ctypes.c_int), ("ProcessLoopbackParams", _PLP)]


class _PV(ctypes.Structure):
    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("r1", ctypes.c_ushort),
        ("r2", ctypes.c_ushort),
        ("r3", ctypes.c_ushort),
        ("cbSize", ctypes.c_ulong),
        ("pBlobData", c_void_p),
    ]


def _make_pv(pid: int):
    params = _AP(ActivationType=1, ProcessLoopbackParams=_PLP(TargetProcessId=pid, ProcessLoopbackMode=0))
    pv = _PV()
    pv.vt = VT_BLOB
    pv.cbSize = ctypes.sizeof(params)
    pv.pBlobData = ctypes.cast(byref(params), c_void_p)
    pv._keep = params  # type: ignore[attr-defined]
    return pv


def main() -> int:
    t, q = ctypes.c_uint(), ctypes.c_uint()
    ole32.CoGetApartmentType(byref(t), byref(q))
    names = {0: "STA", 1: "MTA", 2: "NA", 3: "MAIN_STA", 4: "IMPLICIT_MTA"}
    print(f"apartment: {names.get(t.value, t.value)} (qualifier={q.value})")
    print("(no comtypes imported -- this probe owns its own MTA)\n")

    fn = ctypes.windll.mmdevapi.ActivateAudioInterfaceAsync
    fn.restype = HRESULT
    fn.argtypes = [c_wchar_p, POINTER(GUID), POINTER(_PV), c_void_p, POINTER(c_void_p)]

    pid = ctypes.windll.kernel32.GetCurrentProcessId()

    cases = [
        ("real endpoint + NULL params", DEVINTERFACE_AUDIO_RENDER, None),
        ("VAD + process-loopback params", VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, _make_pv(pid)),
    ]
    ok_any = False
    for label, path, params in cases:
        handler = AgileHandler()  # keep a reference for the whole call
        op = c_void_p()
        try:
            hr = fn(
                path,
                byref(IID_IAUDIO_CLIENT),
                byref(params) if params is not None else None,
                handler.pointer,
                byref(op),
            )
            good = hr == 0
            ok_any = ok_any or good
            print(f"  {label:32} hr=0x{hr & 0xFFFFFFFF:08X}  {'OK' if good else 'FAIL'}")
        except OSError as exc:
            print(f"  {label:32} OSError 0x{exc.winerror & 0xFFFFFFFF:08X}")
        if handler.done.wait(5.0):
            print(f"      callback fired; operation={'set' if handler.operation else 'missing'}")
        else:
            print("      callback did NOT fire within 5s")
    return 0 if ok_any else 1


if __name__ == "__main__":
    sys.exit(main())
