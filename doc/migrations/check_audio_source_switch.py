"""End-to-end check of the AudioCapture per-process source.

Tests the *integration*, not just the engine: AudioCapture is started with
device loopback disabled, pointed at a specific executable, and the audio it
delivers through the normal `get_audio()` queue is measured. Then it is switched
to a different executable to confirm the source really follows the target.

Two executables with unique names are used for the target and the bystander.
That is not cosmetic: the obvious choice, python.exe, would also match this
process and any other Python on the machine, and `find_pids_by_name` would have
to guess.

Run (from a directory that can write next to the env's python):
    python doc/migrations/check_audio_source_switch.py
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TONE_SECONDS = 40
READ_SECONDS = 2.0

PLAYER_SRC = (
    "import sys, time, winsound\n"
    "winsound.PlaySound(sys.argv[1], winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)\n"
    "print('ready', flush=True)\n"
    f"time.sleep({TONE_SECONDS})\n"
)
SILENT_SRC = f"import time; time.sleep({TONE_SECONDS})"


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


def _drain(capture, seconds: float):
    """Collect audio from the queue for a while; returns (rms, chunks)."""
    import numpy as np

    pieces = []
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        item = capture.get_audio(timeout=0.2)
        if item is None:
            continue
        audio, _mic = item
        if audio is not None and len(audio):
            pieces.append(np.asarray(audio, dtype=np.float32))
    if not pieces:
        return -1.0, 0
    data = np.concatenate(pieces)
    return float(np.sqrt(np.mean(data**2))), len(pieces)


def main() -> int:
    from livetranslate.audio.capture import AudioCapture
    from livetranslate.audio import proc_loopback as plp

    if not plp.available():
        print(f"SKIP: needs Windows build >= {plp.MIN_WINDOWS_BUILD}")
        return 1

    py = sys.executable
    env_dir = os.path.dirname(py)
    work = tempfile.mkdtemp(prefix="lt-switch-")

    # Unique executable names so process lookup cannot pick the wrong pid.
    target_exe = os.path.join(env_dir, "lt_tone_probe.exe")
    silent_exe = os.path.join(env_dir, "lt_silent_probe.exe")
    for src, dst in ((py, target_exe), (py, silent_exe)):
        shutil.copy2(src, dst)

    wav = os.path.join(work, "tone.wav")
    _make_tone(wav)

    player = bystander = None
    capture = None
    try:
        print("[1/4] launching a tone player and a silent bystander")
        player = subprocess.Popen(
            [target_exe, "-c", PLAYER_SRC, wav],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        bystander = subprocess.Popen([silent_exe, "-c", SILENT_SRC],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"  {os.path.basename(target_exe)} pid={player.pid}  "
              f"{os.path.basename(silent_exe)} pid={bystander.pid}")
        if player.stdout:
            print(f"  player says: {player.stdout.readline().strip()}")
        time.sleep(1.5)

        print("\n[2/4] AudioCapture scoped to the TONE PLAYER -- expect NON-silent")
        capture = AudioCapture(device="__disabled__", sample_rate=16000, chunk_duration=0.032)
        capture.set_target_process(os.path.basename(target_exe))
        capture.start()
        time.sleep(1.0)
        rms_target, n1 = _drain(capture, READ_SECONDS)
        print(f"  rms={rms_target:.5f}  chunks={n1}  (source={capture.target_process})")

        print("\n[3/4] switching to the SILENT BYSTANDER -- expect SILENT")
        capture.set_target_process(os.path.basename(silent_exe))
        time.sleep(1.5)  # let the read loop perform the switch
        print(f"  active source is now: {capture.target_process}")
        rms_silent, n2 = _drain(capture, READ_SECONDS)
        print(f"  rms={rms_silent:.5f}  chunks={n2}")

        print("\n[4/4] switching back to device-wide capture")
        capture.set_target_process(None)
        time.sleep(1.5)
        print(f"  active source is now: {capture.target_process}  (None == device)")

        ok_target = rms_target > 0.001
        ok_switch = capture.target_process is None
        print("\n=== RESULT ===")
        print(f"  process source delivered audio : {'PASS' if ok_target else 'FAIL'}")
        print(f"  source follows the target      : PASS" if rms_silent < 0.001 else
              f"  source follows the target      : FAIL (rms {rms_silent:.5f})")
        print(f"  switched back to device        : {'PASS' if ok_switch else 'FAIL'}")
        if ok_target and rms_silent < 0.001 and ok_switch:
            print("\nOK: AudioCapture switches between device-wide and per-process capture.")
            return 0
        print("\nFAIL: see numbers above")
        return 1
    finally:
        if capture is not None:
            try:
                capture.stop()
            except Exception:  # noqa: BLE001
                pass
        for proc in (player, bystander):
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
        for path in (target_exe, silent_exe):
            try:
                os.remove(path)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
