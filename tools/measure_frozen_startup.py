"""Measure where a frozen one-file build actually spends its startup time.

`tools/verify_frozen.py` proves correctness but cannot attribute latency: main.py
imports torch and PyQt6 at module level, so anything the probe imports is
already cached. The interesting numbers are therefore:

  * bare boot      -- unpack the archive and reach the probe, nothing else
  * worker respawn -- a multiprocessing spawn child is a *second full launch*
                      of the same exe; for one-file builds that means a second
                      unpack, unless PyInstaller reuses the parent's dir

Run with an env var holding probe source, exactly like verify_frozen.py.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]

# Phase 1: reach Python inside the exe. Phase 2: spawn the ASR worker.
BOOT_PROBE = r"""
import json, os, sys, time
print("BOOT_REPORT:" + json.dumps({
    "exe": sys.executable,
    "frozen": bool(getattr(sys, "frozen", False)),
    "meipass": getattr(sys, "_MEIPASS", ""),
    "pid": os.getpid(),
}))
sys.stdout.flush()
"""

WORKER_PROBE = r"""
import json, os, sys, time
t = time.time()
from livetranslate.asr.client import ASRClient
client = ASRClient({"engine_type": "echo"})
client.start()
pid = client.pid
client.terminate()
print("WORKER_REPORT:" + json.dumps({
    "spawn_s": round(time.time() - t, 1),
    "child_pid": pid,
}))
sys.stdout.flush()
"""


def run(exe: Path, probe: str) -> tuple[dict, float]:
    env = dict(os.environ)
    env["LIVETRANSLATE_PROBE"] = probe
    started = time.time()
    proc = subprocess.run(
        [str(exe)], cwd=str(exe.parent), env=env,
        capture_output=True, text=True, timeout=1800,
    )
    elapsed = time.time() - started
    for line in (proc.stdout or "").splitlines():
        if line.startswith("{"):
            pass
    marker = "REPORT:"
    for line in (proc.stdout or "").splitlines():
        if marker in line:
            return json.loads(line.split(marker, 1)[1]), elapsed
    raise SystemExit(
        f"probe produced no report (rc={proc.returncode})\n"
        f"stdout: {(proc.stdout or '')[-1500:]}\n"
        f"stderr: {(proc.stderr or '')[-1500:]}"
    )


def main() -> int:
    exe = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else PROJECT_DIR / "LiveTranslate.exe"
    if not exe.is_file():
        raise SystemExit(f"not found: {exe}")

    print("=" * 68)
    print("  Frozen startup breakdown")
    print("=" * 68)
    print(f"  exe: {exe}")
    print(f"  size: {exe.stat().st_size / 1024 ** 3:.2f} GB")
    print(f"  one-file: {'no' if exe.parent.name == 'LiveTranslate' else 'yes'}")
    print()

    print("  [1/2] bare boot (unpack only)...")
    boot, boot_wall = run(exe, BOOT_PROBE)
    print(f"        {boot_wall:.1f}s  pid={boot['pid']}")
    print(f"        _MEIPASS = {boot['meipass']}")
    print()

    print("  [2/2] boot + spawn an ASR worker...")
    worker, worker_wall = run(exe, WORKER_PROBE)
    delta = worker_wall - boot_wall
    print(f"        {worker_wall:.1f}s total, worker spawn {worker['spawn_s']}s")
    print(f"        child pid={worker['child_pid']}")
    print()

    print("=" * 68)
    print(f"  unpack + interpreter : {boot_wall:6.1f}s")
    print(f"  first worker spawn   : {delta:6.1f}s   (second launch of the exe)")
    print(f"  total to first use   : {worker_wall:6.1f}s")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
