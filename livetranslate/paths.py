"""Central path anchors for LiveTranslate.

Layout rule: **code lives inside the ``livetranslate`` package; runtime data
lives at the project root.** Everything that used to be computed as
``Path(__file__).parent`` from a flat top-level module must now go through this
module, otherwise moving a file one level deeper silently breaks it (e.g.
``MODELS_DIR`` would become ``livetranslate/models/models``).

Layout::

    <APP_ROOT>/
    |- main.py
    |- config.yaml        <- CONFIG_FILE
    |- user_settings.json <- SETTINGS_FILE
    |- i18n/              <- I18N_DIR
    |- models/            <- MODELS_DIR
    |- logs/              <- LOGS_DIR
    |- transcripts/       <- TRANSCRIPTS_DIR
    `- livetranslate/     <- code

Frozen (PyInstaller one-file) builds
------------------------------------
``__file__`` then points inside the temporary extraction directory
(``sys._MEIPASS``), which PyInstaller wipes on exit -- reading *and writing*
runtime data there would silently lose every setting, model and transcript.
So when frozen, ``APP_ROOT`` becomes the directory holding the executable:
that keeps the settings file, the model cache, logs and transcripts next to
the exe, which is what lets a packaged build reuse an existing checkout's
data instead of starting from scratch.

``BUNDLE_DIR`` exposes the read-only extraction directory for resources that
*must* ship inside the exe (``i18n/*.yaml``, ``CHANGELOG_*.md``).
"""

import sys
from pathlib import Path

# ── Frozen detection ────────────────────────────────────────────────────────
# PyInstaller sets sys.frozen on both the one-file and one-folder bootloader.
FROZEN: bool = bool(getattr(sys, "frozen", False))

# Where the one-file archive was unpacked. Read-only, deleted on exit.
BUNDLE_DIR: Path = (
    Path(getattr(sys, "_MEIPASS")) if getattr(sys, "_MEIPASS", "") else Path(__file__).resolve().parents[1]
)

if FROZEN:
    # livetranslate/paths.py -> parents[1] = project root
    APP_ROOT: Path = Path(sys.executable).resolve().parent
else:
    # livetranslate/paths.py -> parents[0] = livetranslate/, parents[1] = project root
    APP_ROOT: Path = Path(__file__).resolve().parents[1]

CONFIG_FILE: Path = APP_ROOT / "config.yaml"
SETTINGS_FILE: Path = APP_ROOT / "user_settings.json"
MODELS_DIR: Path = APP_ROOT / "models"
LOGS_DIR: Path = APP_ROOT / "logs"
TRANSCRIPTS_DIR: Path = APP_ROOT / "transcripts"


def _resolve_i18n_dir() -> Path:
    """Prefer an i18n/ directory next to the exe, fall back to the bundle.

    A packaged exe sits in the project root, so the checkout's i18n/ is used
    and translations stay editable without a rebuild. If the exe is copied
    somewhere else (or the checkout is gone), the copy baked into the exe --
    which PyInstaller places under ``BUNDLE_DIR`` -- keeps it working.
    """
    local = APP_ROOT / "i18n"
    if (local / "zh.yaml").is_file():
        return local
    bundled = BUNDLE_DIR / "i18n"
    if (bundled / "zh.yaml").is_file():
        return bundled
    return local


I18N_DIR: Path = _resolve_i18n_dir()


def ensure_runtime_files() -> list[str]:
    """Create missing runtime dirs and seed read-only defaults. Returns notes.

    Only acts when frozen: in a source checkout the project root already has
    everything and creating dirs eagerly would only mask a broken clone.
    ``config.yaml`` ships inside the exe as the default set and is copied out
    on first run if the user has none; ``user_settings.json`` is deliberately
    *not* seeded -- the control panel owns its defaults, and an empty file
    would be read as "user saved nothing" instead of "never configured".
    """
    notes: list[str] = []
    if not FROZEN:
        return notes

    for directory in (MODELS_DIR, LOGS_DIR, TRANSCRIPTS_DIR):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            notes.append(f"cannot create {directory.name}/: {exc}")

    if not CONFIG_FILE.is_file():
        seed = BUNDLE_DIR / "config.yaml"
        if seed.is_file():
            try:
                CONFIG_FILE.write_bytes(seed.read_bytes())
                notes.append("seeded config.yaml from bundle")
            except OSError as exc:
                notes.append(f"cannot write config.yaml: {exc}")
        else:
            notes.append("config.yaml missing and not bundled")
    return notes


__all__ = [
    "APP_ROOT",
    "BUNDLE_DIR",
    "CONFIG_FILE",
    "FROZEN",
    "I18N_DIR",
    "LOGS_DIR",
    "MODELS_DIR",
    "SETTINGS_FILE",
    "TRANSCRIPTS_DIR",
    "ensure_runtime_files",
]
