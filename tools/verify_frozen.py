"""Smoke-test a frozen LiveTranslate build without a GUI or audio hardware.

Checks the things a packaged build actually breaks, which source runs never
exercise: that the executable boots at all, that it finds the project root's
runtime data instead of the temporary extraction directory, and that the
frozen ASR worker subprocess can be spawned and answer over its pipe.

Usage::

    python tools/verify_frozen.py                 # dist/LiveTranslate.exe
    python tools/verify_frozen.py path/to/exe     # explicit path

Not named ``test_*`` on purpose: pytest would import it during collection and
this module loads a 4 GB binary tree.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]


def _default_exe() -> Path:
    candidates = [
        PROJECT_DIR / "dist" / "LiveTranslate.exe",
        PROJECT_DIR / "dist" / "LiveTranslate" / "LiveTranslate.exe",
        PROJECT_DIR / "LiveTranslate.exe",
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise SystemExit(
        "No frozen build found. Build one first:\n"
        "  python build_exe.py            (one-file exe)\n"
        "  python build_exe.py --onedir   (one-folder)\n"
        f"Looked in:\n  " + "\n  ".join(str(c) for c in candidates)
    )


# Runs *inside* the frozen app: printed to stdout, one JSON line per check.
FROZEN_PROBE = r"""
import json, os, sys, time

report = {}

t0 = time.time()
try:
    import torch
    report["torch"] = {
        "ok": True,
        "import_s": round(time.time() - t0, 2),
        "cuda": torch.cuda.is_available(),
    }
    if torch.cuda.is_available():
        report["torch"]["device"] = torch.cuda.get_device_name(0)
except Exception as exc:
    report["torch"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

t1 = time.time()
try:
    import PyQt6.QtWidgets
    report["pyqt"] = {"ok": True, "import_s": round(time.time() - t1, 2)}
except Exception as exc:
    report["pyqt"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

try:
    from livetranslate.paths import (
        APP_ROOT, BUNDLE_DIR, FROZEN, I18N_DIR,
        CONFIG_FILE, SETTINGS_FILE, MODELS_DIR,
    )
    report["paths"] = {
        "frozen": bool(FROZEN),
        "app_root": str(APP_ROOT),
        "app_root_is_meipass": str(APP_ROOT) == str(BUNDLE_DIR),
        "bundle_dir": str(BUNDLE_DIR),
        "config_exists": CONFIG_FILE.is_file(),
        "settings_exists": SETTINGS_FILE.is_file(),
        "i18n_dir": str(I18N_DIR),
        "i18n_zh_exists": (I18N_DIR / "zh.yaml").is_file(),
        "models_dir": str(MODELS_DIR),
        "models_exists": MODELS_DIR.is_dir(),
        "app_root_writable": os.access(str(APP_ROOT), os.W_OK),
    }
except Exception as exc:
    report["paths"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

# The highest-risk frozen failure: the ASR worker is a multiprocessing "spawn"
# child, so the exe must be able to re-enter itself and unpickle worker_main.
try:
    from livetranslate.asr.client import ASRClient
    report["asr_worker_spawn"] = {"ok": True, "pid": None}
    client = ASRClient({"engine_type": "echo"})
    client.start()
    report["asr_worker_spawn"]["pid"] = client.pid
    client.terminate()
except Exception as exc:
    report["asr_worker_spawn"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

print("FROZEN_REPORT:" + json.dumps(report))
"""


def run_probe(exe: Path) -> dict:
    """Execute the frozen app in probe mode and parse its JSON report."""
    env = dict(os.environ)
    env["LIVETRANSLATE_PROBE"] = FROZEN_PROBE

    started = time.time()
    proc = subprocess.run(
        [str(exe)],
        cwd=str(exe.parent),
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    elapsed = time.time() - started

    stdout = proc.stdout or ""
    marker = "FROZEN_REPORT:"
    if marker not in stdout:
        return {
            "ok": False,
            "error": "frozen app produced no probe report (crashed at startup?)",
            "returncode": proc.returncode,
            "stdout_tail": stdout[-2000:],
            "stderr_tail": (proc.stderr or "")[-4000:],
        }

    line = next(l for l in stdout.splitlines() if l.startswith(marker))
    report = json.loads(line[len(marker):])
    report["wall_s"] = round(elapsed, 1)
    return report


def check(name: str, ok: bool, detail: str = "") -> bool:
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" -- {detail}" if detail else ""))
    return ok


def main() -> int:
    exe = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else _default_exe()
    print("=" * 68)
    print(f"  Frozen build verification")
    print("=" * 68)
    print(f"  exe: {exe}")
    print(f"  size: {exe.stat().st_size / 1024 ** 3:.2f} GB")
    print()

    print("  running probe (first launch unpacks everything, be patient)...")
    report = run_probe(exe)
    print(f"  wall clock: {report.get('wall_s', '?')}s")
    print()

    if not report.get("ok", False) and "torch" not in report:
        print("  FAILED before any report was produced:\n")
        print(report.get("stdout_tail", ""))
        print(report.get("stderr_tail", ""))
        return 1

    results: list[bool] = []

    print("  -- interpreter --")
    torch_info = report.get("torch", {})
    results.append(check(
        "torch imports", torch_info.get("ok", False),
        torch_info.get("error", f"{torch_info.get('import_s')}s"),
    ))
    results.append(check(
        "PyQt6 imports", report.get("pyqt", {}).get("ok", False),
        report.get("pyqt", {}).get("error", ""),
    ))
    if torch_info.get("ok"):
        results.append(check(
            "CUDA available", torch_info.get("cuda", False),
            torch_info.get("device", "CPU only"),
        ))

    print("\n  -- paths --")
    paths = report.get("paths", {})
    results.append(check("running frozen", paths.get("frozen", False)))
    results.append(check(
        "APP_ROOT is not the temp extraction dir",
        paths.get("app_root_is_meipass") is False,
        f"APP_ROOT={paths.get('app_root')}",
    ))
    results.append(check(
        "APP_ROOT writable", paths.get("app_root_writable", False),
    ))
    results.append(check("config.yaml found", paths.get("config_exists", False),
                         str(paths.get("app_root"))))
    results.append(check("i18n/zh.yaml found", paths.get("i18n_zh_exists", False),
                         paths.get("i18n_dir", "")))
    results.append(check("models/ found", paths.get("models_exists", False),
                         paths.get("models_dir", "")))

    settings_exists = paths.get("settings_exists", False)
    print(f"  [INFO] user_settings.json {'reused' if settings_exists else 'absent (first run)'}")

    print("\n  -- ASR worker (spawn subprocess) --")
    spawn_info = report.get("asr_worker_spawn", {})
    results.append(check(
        "worker spawns inside the frozen exe", spawn_info.get("ok", False),
        spawn_info.get("error", f"pid={spawn_info.get('pid')}"),
    ))

    failed = results.count(False)
    print()
    print("=" * 68)
    print(f"  {len(results) - failed}/{len(results)} checks passed")
    print("=" * 68)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
