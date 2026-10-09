"""Bisect which argument ActivateAudioInterfaceAsync is rejecting.

E_ILLEGAL_METHOD_CALL can come from the apartment, but it failed on a fresh MTA
thread too, so this separates the candidate causes by trying each argument
combination in a *fresh subprocess* (apartment state is per-process and
comtypes pins the main thread to STA on import, so it cannot be tested in one
interpreter).

  A. real endpoint id + NULL params        -> does the call work at all?
  B. real endpoint id + process-loopback params
  C. VAD\\Process_Loopback + NULL params   -> is it the device path or the params?
  D. VAD\\Process_Loopback + pv + NULL handler  -> is the handler rejected?

Run: python doc/migrations/probe_activation_args.py
"""

import ctypes
import os
import subprocess
import sys
from ctypes import POINTER, byref, c_void_p, c_wchar_p
from ctypes import wintypes

CHILD = r'''
import ctypes, sys
from ctypes import POINTER, byref, c_void_p, c_wchar_p
from ctypes import wintypes

# Initialise COM as MTA *before* comtypes gets a chance to pin this thread to
# STA on import -- that ordering is one of the hypotheses under test.
mode = sys.argv[1]
if mode == "mta_first":
    ctypes.windll.ole32.CoInitializeEx(None, 0x0)   # COINIT_MULTITHREADED == 0
elif mode == "sta_first":
    ctypes.windll.ole32.CoInitializeEx(None, 0x2)   # COINIT_APARTMENTTHREADED == 2

variant = sys.argv[2]

import comtypes
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
from pycaw.api.audioclient import IAudioClient

ole32 = ctypes.windll.ole32
t, q = ctypes.c_uint(), ctypes.c_uint()
ole32.CoGetApartmentType(byref(t), byref(q))
APTTYPES = {0: "STA", 1: "MTA", 2: "NA", 3: "MAIN_STA", 4: "IMPLICIT_MTA"}
apt = APTTYPES.get(t.value, t.value)

VT_BLOB = 65

class PLP(ctypes.Structure):
    _fields_ = [("TargetProcessId", wintypes.DWORD), ("ProcessLoopbackMode", ctypes.c_int)]

class AP(ctypes.Structure):
    _fields_ = [("ActivationType", ctypes.c_int), ("ProcessLoopbackParams", PLP)]

class PV(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                ("r3", ctypes.c_ushort), ("cbSize", ctypes.c_ulong), ("pBlobData", c_void_p)]

class IOp(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = (COMMETHOD([], HRESULT, "GetActivateResult",
                           (["out"], POINTER(HRESULT), "a"),
                           (["out"], POINTER(POINTER(IUnknown)), "b")),)

class IHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = (COMMETHOD([], HRESULT, "ActivateCompleted", (["in"], POINTER(IOp), "o")),)

class H(comtypes.COMObject):
    _com_interfaces_ = [IHandler]
    def ActivateCompleted(self, o):
        return 0

def make_pv(pid):
    p = AP(ActivationType=1, ProcessLoopbackParams=PLP(TargetProcessId=pid, ProcessLoopbackMode=0))
    pv = PV(); pv.vt = VT_BLOB; pv.cbSize = ctypes.sizeof(p); pv.pBlobData = ctypes.cast(byref(p), c_void_p)
    pv._keep = p
    return pv

# a real render endpoint id, for the control cases
from pycaw.pycaw import AudioUtilities
dev_id = AudioUtilities.GetSpeakers().id

fn = ctypes.windll.mmdevapi.ActivateAudioInterfaceAsync
fn.restype = HRESULT
fn.argtypes = [c_wchar_p, POINTER(GUID), POINTER(PV), POINTER(IHandler), POINTER(POINTER(IOp))]

VAD = "VAD\\Process_Loopback"
pid = ctypes.windll.kernel32.GetCurrentProcessId()

if variant == "A":
    path, params, handler = dev_id, None, H()
elif variant == "B":
    path, params, handler = dev_id, make_pv(pid), H()
elif variant == "C":
    path, params, handler = VAD, None, H()
else:
    path, params, handler = VAD, make_pv(pid), None

op = POINTER(IOp)()
try:
    hr = fn(path, byref(IAudioClient._iid_),
            byref(params) if params is not None else None,
            handler.QueryInterface(IHandler) if handler is not None else None,
            byref(op))
    print(f"RESULT apt={apt} variant={variant} hr=0x{hr & 0xFFFFFFFF:08X}")
except OSError as e:
    print(f"RESULT apt={apt} variant={variant} OSError 0x{e.winerror & 0xFFFFFFFF:08X}")
except Exception as e:
    print(f"RESULT apt={apt} variant={variant} {type(e).__name__}: {e}")
'''


def main() -> int:
    py = sys.executable
    here = os.path.dirname(os.path.abspath(__file__))
    tmp = os.path.join(here, "_child_bisect.py")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(CHILD)

    for mode in ("default", "mta_first"):
        print(f"\n### COM init: {mode} ###")
        for variant in ("A", "B", "C", "D"):
            r = subprocess.run(
                [py, "-u", tmp, mode, variant],
                capture_output=True, text=True, timeout=90, cwd=os.getcwd(),
            )
            out = (r.stdout or "").strip().splitlines()
            line = next((ln for ln in out if ln.startswith("RESULT")), None)
            if line:
                print("  " + line)
            else:
                err = (r.stderr or "").strip().splitlines()
                print(f"  variant={variant} no result; {err[-1] if err else 'no output'}")
    try:
        os.remove(tmp)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
