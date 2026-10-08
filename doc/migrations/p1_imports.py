#!/usr/bin/env python3
"""P1 migration: rewrite flat-module imports to package-qualified imports.

One-shot and deterministic: an explicit mapping table, no fuzzy matching.
The mapping is keyed on the *old top-level module name*, which is unique per
module, so it also handles multi-line ``from x import (\n ... )`` statements
(only the first line carries the module name).

Deliberately NOT touched:
  * imports already starting with ``livetranslate.``
  * bare vendored imports inside ``nano_src`` (``import model``, ``from ctc``,
    ``from tools.utils``) -- they resolve via sys.path injection, see
    ``livetranslate/asr/backends/funasr_nano.py``.
  * third-party imports (torch, PyQt6, ...).

Run from the project root:  python doc/migrations/p1_imports.py [--dry-run]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MODULE_MAP: dict[str, str] = {
    # audio
    "audio_capture": "livetranslate.audio.capture",
    "vad_processor": "livetranslate.audio.vad",
    # asr
    "asr_client": "livetranslate.asr.client",
    "asr_worker": "livetranslate.asr.worker",
    "asr_server": "livetranslate.asr.server",
    "asr_engine": "livetranslate.asr.backends.whisper",
    "asr_funasr": "livetranslate.asr.backends.funasr",
    "asr_sensevoice": "livetranslate.asr.backends.sensevoice",
    "asr_funasr_nano": "livetranslate.asr.backends.funasr_nano",
    "asr_anime_whisper": "livetranslate.asr.backends.anime_whisper",
    "asr_remote": "livetranslate.asr.backends.remote",
    # translate
    "translator": "livetranslate.translate.translator",
    # output
    "transcript_writer": "livetranslate.output.transcript",
    # ui
    "subtitle_overlay": "livetranslate.ui.overlay",
    "subtitle_window": "livetranslate.ui.subtitle_window",
    "subtitle_settings": "livetranslate.ui.subtitle_settings",
    "control_panel": "livetranslate.ui.control_panel",
    "dialogs": "livetranslate.ui.dialogs",
    "log_window": "livetranslate.ui.log_window",
    "i18n": "livetranslate.ui.i18n",
    # models
    "model_manager": "livetranslate.models.manager",
    # misc
    "benchmark": "livetranslate.benchmark",
}

# ``from <mod> import ...``  (the module token is the only thing before ``import``)
FROM_RE = re.compile(r"^(\s*from\s+)([A-Za-z_]\w*)(\s+import\b.*)$")
# ``import <mod>`` or ``import <mod> as <alias>``
IMPORT_RE = re.compile(r"^(\s*import\s+)([A-Za-z_]\w*)((?:\s+as\s+\w+)?\s*)$")

SKIP_DIRS = {".git", "__pycache__", ".venv", "models", "logs", "release", "doc"}


def rewrite_line(line: str, stats: dict, path: Path):
    m = FROM_RE.match(line)
    if m and m.group(2) in MODULE_MAP:
        if "livetranslate" not in line:
            stats["from"] += 1
            return f"{m.group(1)}{MODULE_MAP[m.group(2)]}{m.group(3)}"
        return line
    m = IMPORT_RE.match(line)
    if m and m.group(2) in MODULE_MAP:
        if "livetranslate" not in line:
            stats["import"] += 1
            return f"{m.group(1)}{MODULE_MAP[m.group(2)]}{m.group(3)}"
        return line
    return line


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = Path.cwd()
    targets = [
        p
        for p in root.rglob("*.py")
        if not any(part in SKIP_DIRS for part in p.relative_to(root).parts)
    ]

    stats = {"from": 0, "import": 0}
    changed_files: list[str] = []

    for path in sorted(targets):
        # nano_src is vendored code: never rewrite
        if "nano_src" in path.relative_to(root).parts:
            continue
        original = path.read_text(encoding="utf-8")
        new = "\n".join(rewrite_line(ln, stats, path) for ln in original.split("\n"))
        if new != original:
            changed_files.append(str(path.relative_to(root)))
            if not args.dry_run:
                path.write_text(new, encoding="utf-8", newline="")

    verb = "would rewrite" if args.dry_run else "rewrote"
    print(f"{verb} {stats['from']} 'from' + {stats['import']} 'import' lines "
          f"across {len(changed_files)} files")
    for f in changed_files:
        print(f"  - {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
