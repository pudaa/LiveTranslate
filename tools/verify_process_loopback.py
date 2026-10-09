"""End-to-end verification of process loopback (option C for audio sources).

Proves the two things that matter, against real audio from a real process:

  1. the target PID's audio is captured (non-silent), and
  2. an unrelated PID's audio is *not* (silent)

The second half is the point. Non-silence alone only shows "something was
captured"; silence-from-everyone-else is what shows the capture is isolated to
one process, which is the whole reason for using process loopback instead of
endpoint loopback plus a second output device.

The ToneWAV is written at ~12% amplitude on purpose: the test only needs a
non-silent signal and does not need to be loud in the room.

Run with the project env python:
    python doc/migrations/check_process_loopback.py
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TONE_SECONDS = 20
CAPTURE_SECONDS = 2.0


def _make_tone(path: str, freq: int = 440, seconds: int = 3, rate: int = 44100) -> None:
    import math

    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        frames = bytearray()
        for i in range(rate * seconds):
            frames += struct.pack("<h", int(4000 * math.sin(2 * math.pi * freq * i / rate)))
        wf.writeframes(bytes(frames))


PLAYER_SRC = (
    "import sys, time, winsound\n"
    "winsound.PlaySound(sys.argv[1], winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)\n"
    "print('ready', flush=True)\n"
    f"time.sleep({TONE_SECONDS})\n"
)


def _rms(raw: bytes) -> float:
    import numpy as np

    if not raw:
        return -1.0
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(data**2))) if data.size else -1.0


def main() -> int:
    from livetranslate.audio import proc_loopback as plp

    print(f"windows build : {plp.windows_build()}  (needs >= {plp.MIN_WINDOWS_BUILD})")
    if not plp.available():
        print("FAIL: this Windows build cannot do process loopback")
        return 1

    tmp = tempfile.mkdtemp(prefix="lt-plb-")
    wav = os.path.join(tmp, "tone.wav")
    _make_tone(wav)

    py = sys.executable
    print("\n[1/3] spawning a player and a silent bystander")
    player = subprocess.Popen(
        [py, "-c", PLAYER_SRC, wav],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    bystander = subprocess.Popen(
        [py, "-c", f"import time; time.sleep({TONE_SECONDS})"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    rc = 1
    try:
        line = player.stdout.readline().strip() if player.stdout else ""
        print(f"  player    pid={player.pid}  ({line})")
        print(f"  bystander pid={bystander.pid}")
        time.sleep(1.5)  # let the audio session come up

        print("\n[2/3] capturing the PLAYER -- expect NON-silent")
        with plp.ProcessLoopbackCapture(player.pid) as cap:
            print(
                f"  format: {cap.SAMPLE_RATE}Hz {cap.CHANNELS}ch {cap.BITS}bit "
                f"(requested directly, no resampling)"
            )
            raw = cap.read_for(CAPTURE_SECONDS)
            rms_player = _rms(raw)
        print(f"  captured {len(raw)} bytes  rms={rms_player:.5f}")

        print("\n[3/3] capturing the BYSTANDER -- expect SILENT (this is the isolation proof)")
        with plp.ProcessLoopbackCapture(bystander.pid) as cap2:
            raw2 = cap2.read_for(CAPTURE_SECONDS)
            rms_bystander = _rms(raw2)
        print(f"  captured {len(raw2)} bytes  rms={rms_bystander:.5f}")

        ok_target = rms_player > 0.001
        # -1 means "no packets at all", which also counts as silent here.
        ok_isolated = rms_bystander < 0.001

        print("\n=== RESULT ===")
        print(f"  target captured     : {'PASS' if ok_target else 'FAIL'}")
        print(f"  isolation held      : {'PASS' if ok_isolated else 'FAIL'}")
        if ok_target and ok_isolated:
            print("\nOK: process loopback captures one process and excludes others.")
            rc = 0
        else:
            print("\nFAIL: see numbers above")
    finally:
        for p in (player, bystander):
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
