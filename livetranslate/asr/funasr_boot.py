"""Import FunASR without paying for the models we never load.

Why this exists
---------------
``funasr/__init__.py`` ends with::

    import_submodules(__name__)

which walks the package with ``pkgutil.walk_packages`` and imports **every
submodule, recursively** -- 432 files. That pulls in nine other model families
(glm_asr, llm_asr, qwen_audio, moss_transcribe_diarize, fun_asr_nano,
hf_tokenizer, ...) and the speaker-clustering backend, and with them
``transformers`` and ``sklearn``/``scipy``.

Measured on this machine, importing ``funasr`` that way costs ~32s, of which:

    transformers (9 model families)      ~16s
    sklearn -> scipy.stats (campplus)     ~6.5s
    the rest of the walk                  ~9s

while the model actually being loaded -- SenseVoice -- imports only numpy,
torch, and funasr's own utilities. It never touches transformers, sklearn or
scipy. So the bulk of that time buys us nothing.

``AutoModel`` is already exported lazily (``_LAZY_EXPORTS`` + module
``__getattr__``), but the eager ``import_submodules`` call defeats it.

What this does
--------------
Installs stub modules for the unused optional dependencies *before* ``funasr``
is imported. ``import_submodules`` swallows the resulting ImportError, records it
in ``funasr.get_import_errors()`` and carries on -- that is its documented
behaviour -- so the modules needing those libraries are skipped (38 of them for
SenseVoice) while the registry still gets populated with the models that matter.

Net effect: ``import funasr`` 32s -> ~10s, and the total worker cold start
43s -> ~18s, with the model loading and transcribing normally.

Caveat
------
The stubs are process-global. That is fine here because the ASR engines are only
ever built inside the worker process, which does nothing else (see the note in
the project docs about engines never being instantiated in the GUI process).

Model families that genuinely need transformers -- fun_asr_nano, llm_asr,
glm_asr, qwen_audio -- must pass ``allow_transformers=True``.
"""

import os
import sys
import types

log_name = "LiveTranslate.FunasrBoot"

# Opt out if a future funasr release starts needing one of these on the path we
# use: set LIVETRANS_FUNASR_BLOCK_DEPS=0.
ENV_DISABLE = "LIVETRANS_FUNASR_BLOCK_DEPS"

# Only ever used by funasr's speaker clustering and EER metrics, neither of which
# any ASR model needs.
ALWAYS_BLOCKED = (
    "sklearn",
    "sklearn.cluster",
    "sklearn.preprocessing",
    "sklearn.metrics",
    "scipy",
    "scipy.stats",
    "scipy.sparse",
    "scipy.linalg",
)

# Imported only by model families other than the ASR ones we run.
TRANSFORMERS_BLOCKED = ("transformers", "transformers.utils", "transformers.models")


class _BlockedModule(types.ModuleType):
    """A stand-in that behaves like an installed-but-unusable package.

    Importing it succeeds so that a module-level ``import transformers`` does not
    abort the import of an unrelated file, and ``__version__`` answers so version
    probes do not trip over it. Reaching for anything else raises, which is what
    makes ``import_submodules`` skip the module that needed it.
    """

    _blocked_version = "0.0.0-blocked"

    def __getattr__(self, name):
        if name == "__version__":
            return self._blocked_version
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        raise ImportError(
            f"{self.__name__}.{name} is unavailable: "
            "LiveTranslate deliberately blocks this dependency to keep the "
            "FunASR import fast."
        )


def _block(names) -> list:
    blocked = []
    for name in names:
        if name in sys.modules:
            continue
        module = _BlockedModule(name)
        module.__file__ = "<blocked>"
        # Empty __path__: submodules cannot be imported through it either.
        module.__path__ = []
        sys.modules[name] = module
        blocked.append(name)
    return blocked


def block_unused_deps(allow_transformers: bool = False) -> list:
    """Stub out the FunASR dependencies the chosen model family never touches."""
    if os.environ.get(ENV_DISABLE, "1") == "0":
        return []
    names = ALWAYS_BLOCKED if allow_transformers else ALWAYS_BLOCKED + TRANSFORMERS_BLOCKED
    return _block(names)


def is_available() -> bool:
    """True when the funasr package is importable at all."""
    import importlib.util

    return importlib.util.find_spec("funasr") is not None


def import_auto_model(allow_transformers: bool = False):
    """Import ``funasr.AutoModel`` with the unused dependencies stubbed out.

    Returns the class; raises whatever the funasr import raises if it is
    genuinely unavailable.
    """
    blocked = block_unused_deps(allow_transformers=allow_transformers)
    if blocked:
        import logging

        logging.getLogger(log_name).info(
            "Blocking %d unused FunASR dependency module(s) to speed up the "
            "import: %s",
            len(blocked),
            ", ".join(blocked),
        )
    from funasr import AutoModel

    return AutoModel


def record_skipped() -> int:
    """How many funasr modules were skipped because of the stubs (0 if unsupported)."""
    try:
        import funasr

        return len(funasr.get_import_errors())
    except Exception:  # noqa: BLE001 - diagnostics only
        return 0
