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
"""

from pathlib import Path

# livetranslate/paths.py -> parents[0] = livetranslate/, parents[1] = project root
APP_ROOT: Path = Path(__file__).resolve().parents[1]

CONFIG_FILE: Path = APP_ROOT / "config.yaml"
SETTINGS_FILE: Path = APP_ROOT / "user_settings.json"
I18N_DIR: Path = APP_ROOT / "i18n"
MODELS_DIR: Path = APP_ROOT / "models"
LOGS_DIR: Path = APP_ROOT / "logs"
TRANSCRIPTS_DIR: Path = APP_ROOT / "transcripts"

__all__ = [
    "APP_ROOT",
    "CONFIG_FILE",
    "SETTINGS_FILE",
    "I18N_DIR",
    "MODELS_DIR",
    "LOGS_DIR",
    "TRANSCRIPTS_DIR",
]
