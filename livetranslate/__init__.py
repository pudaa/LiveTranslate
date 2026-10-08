"""LiveTranslate - real-time system-audio captioning and translation for Windows.

Layout rule: **code lives in this package; runtime data lives at the project
root.** See :mod:`livetranslate.paths` for the canonical path anchors.

Keep this module import-light. ``main.py`` imports it (transitively, via
``livetranslate.paths`` / ``livetranslate.models.manager``) *before* importing
torch, and importing torch before PyQt6 is required to avoid a DLL conflict on
Windows -- so nothing here may pull in torch or PyQt6 eagerly.
"""

__version__ = "0.1.0"
