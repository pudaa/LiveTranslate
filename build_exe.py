"""PyInstaller one-file build for LiveTranslate.

Produces ``dist/LiveTranslate.exe`` -- a single self-contained executable that
keeps all runtime data (settings, models, logs, transcripts) next to itself
instead of inside PyInstaller's temporary extraction directory. See
``livetranslate/paths.py`` for how ``APP_ROOT`` is resolved when frozen.

Why the exclusion rules matter
------------------------------
* ``torch`` ships ~780 MB of ``.lib`` static import libraries (``dnnl.lib``
  alone is 623 MB). They are link-time only; the runtime loads the matching
  ``.dll``. Keeping them would nearly double the exe for nothing.
* ``conda``/``IPython``/``notebook``/``matplotlib``-style trees come from the
  conda-forge base env rather than from ``requirements.txt``, and nothing in
  the app imports them.
* ``funasr``/``transformers`` resolve backends by importing optional packages
  at runtime, so they must stay importable or model loading fails at run time
  instead of at build time.

Run with::

    python build_exe.py            # one-file exe into dist/
    python build_exe.py --onedir   # faster-to-start folder build, for testing
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
ONEDIR = "--onedir" in sys.argv
PACKAGE = "--package" in sys.argv
# Re-zip an existing dist/ without paying the ~3 minute PyInstaller pass again.
PACKAGE_ONLY = "--package-only" in sys.argv

# Never let a stray one-file build land inside the project it builds from.
WORK_DIR = PROJECT_DIR / "build" / "pyinstaller"
DIST_DIR = PROJECT_DIR / "dist"
APP_NAME = "LiveTranslate"

SP = Path(sys.prefix) / "Lib" / "site-packages"

# ── Build flavour ───────────────────────────────────────────────────────────
# The release build is **CPU-only**. Inference is sherpa-onnx (ONNX Runtime) and
# the VAD is ONNX too, so torch is not needed at runtime -- and on the machines
# the release targets it is not installed at all. Excluding it is therefore not
# an optimisation but a correctness requirement: a stray `import torch` inside
# the frozen app must raise ImportError, which the code is now written to
# tolerate (see the try/except in main.py and audio/vad.py).
#
# Pass --with-torch to build the old CUDA-capable exe for local use.
LITE = "--with-torch" not in sys.argv

# What a lite build drops. Torch-based ASR backends first -- they cannot run
# without torch anyway -- then the media-decoding stack: librosa pulls in
# numba / llvmlite / scipy (well over 100MB of wheels) purely to read mp3 and
# flac files, while the capture path only ever sees raw 16kHz PCM.
LITE_EXCLUDES = [
    "torch", "torchaudio", "torchvision",
    "funasr", "ctranslate2", "faster_whisper", "openai_whisper", "whisper",
    "transformers", "tokenizers", "safetensors",
    "modelscope", "huggingface_hub", "datasets",
    "silero_vad",
    "librosa", "soundfile", "audioread", "soxr", "pydub",
]

# ── Data that must travel inside the exe ────────────────────────────────────
# models/ (897 MB), logs/, transcripts/ and user_settings.json are
# deliberately absent: they are runtime data and are reused from the project
# root, which is where the frozen APP_ROOT points.
#
# The ONNX models are ~310MB (SenseVoice 233MB + punctuation 75MB + VAD 2.3MB).
# Even if they fitted, putting them in a one-file exe would make every launch
# unpack them into a temp dir -- so they ship *next to* the exe and are picked
# up through APP_ROOT. See build_release.ps1 for the packaging step.
BUNDLED_DATA = [
    (PROJECT_DIR / "config.yaml", "."),
    (PROJECT_DIR / "i18n", "i18n"),
]

BINARIES: list[tuple[str, str]] = []

# The whole CUDA runtime is loaded by torch at run time via plain LoadLibrary,
# so PyInstaller's dependency analysis cannot see it.
# The CUDA runtime and ctranslate2 are invisible to PyInstaller's dependency
# walk (torch loads them with a plain LoadLibrary), so they must be listed by
# hand -- but only for a --with-torch build.
if not LITE:
    if (SP / "torch" / "lib").is_dir():
        BINARIES += [
            (str(dll), "torch/lib")
            for dll in (SP / "torch" / "lib").glob("*.dll")
        ]
    if (SP / "torchaudio" / "lib").is_dir():
        BINARIES += [
            (str(dll), "torchaudio/lib")
            for dll in (SP / "torchaudio" / "lib").glob("*.dll")
        ]
    if (SP / "ctranslate2").is_dir():
        BINARIES += [
            (str(p), "ctranslate2") for p in (SP / "ctranslate2").rglob("*.dll")
        ]

# sherpa-onnx ships its own onnxruntime.dll plus the C and C++ APIs; collect the
# whole lib/ tree. The standalone onnxruntime package (used by the VAD) is
# gathered by PyInstaller's own hook, since it is an ordinary hidden import.
if (SP / "sherpa_onnx").is_dir():
    BINARIES += [
        (str(p), f"sherpa_onnx/{p.parent.name}")
        for p in (SP / "sherpa_onnx").rglob("*.dll")
    ]

# ── conda's Library/bin ─────────────────────────────────────────────────────
# conda keeps the DLLs that CPython's extension modules link against in
# Library/bin, which is NOT on PyInstaller's dependency-walk path. Without them
# the frozen app dies at startup with:
#
#     ImportError: DLL load failed while importing _ssl: 找不到指定的程序。
#
# reached as main -> translator -> openai -> anyio -> ssl. _hashlib, _lzma,
# _sqlite3, _ctypes and friends have the same problem. It is only ~21MB in
# total, so the whole directory is collected rather than guessing which
# extension needs which library.
_CONDA_DLL_SKIP = {
    "ucrtbase.dll",  # ships with Windows; bundling it risks version mismatch
    "tcl86t.dll", "tk86t.dll", "tclpip86t.dll",  # Tk stack, excluded above
}
CONDA_LIB_BIN = Path(sys.prefix) / "Library" / "bin"
if CONDA_LIB_BIN.is_dir():
    BINARIES += [
        (str(dll), ".")
        for dll in sorted(CONDA_LIB_BIN.glob("*.dll"))
        if dll.name.lower() not in _CONDA_DLL_SKIP
    ]

def _collect_submodules(package: str) -> list[str]:
    """Every module inside ``package``, as dotted names.

    PyInstaller cannot find these on its own when a package loads its submodules
    dynamically -- which is exactly what yasbd does: it imports ``rules/<lang>.py``
    per language code, and lists that directory to report which languages it
    supports. Neither the import nor the directory is visible to static analysis.
    """
    root = SP / package
    if not root.is_dir():
        return []
    names = []
    for path in sorted(root.rglob("*.py")):
        parts = list(path.relative_to(SP).with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
        if parts:
            names.append(".".join(parts))
    return names


_YASBD_MODULES = _collect_submodules("yasbd")

# ── Hidden imports ──────────────────────────────────────────────────────────
# Imported dynamically (str names, entry points, backend registries) or only
# reachable through a backend's own loader, so static analysis misses them.
_HIDDEN_BASE = [
    # Reached by string: the worker entry point and the backend package.
    "livetranslate.asr.worker",
    "livetranslate.asr.backends",
    # ⚠️ The registry resolves backends with importlib.import_module(<string>),
    # so PyInstaller's dependency walk CANNOT see them. Omitting these produces
    # an exe that starts, gets through setup, and then fails the moment it loads
    # a model with:
    #
    #     ASR 模型加载失败: No module named 'livetranslate.asr.backends.sherpa_onnx'
    #
    # which is exactly what the first build shipped. Listing the package alone
    # is not enough -- its __init__ imports nothing.
    "livetranslate.asr.backends.sherpa_onnx",  # the default engine
    "livetranslate.asr.backends.remote",  # remote-whisper, always selectable
    # ONNX inference -- sherpa-onnx for ASR, onnxruntime for the VAD.
    "sherpa_onnx",
    "onnxruntime",
    "onnxruntime.capi._pybind_state",
    # sentence splitting for incremental ASR
    "yasbd",
    # translation client
    "openai",
    "httpx",
    "httpcore",
    # misc runtime deps reached dynamically
    "yaml",
    "numpy",
    "requests",
    "urllib3",
    "certifi",
    "srt",
    "sounddevice",
    "webrtcvad",
    "pyaudiowpatch",
    "comtypes",
    "pythoncom",
    "win32com",
    "pywintypes",
    "win32api",
    "win32file",
    "win32pipe",
    "win32process",
    "json_repair",
    "langdetect",
    "charset_normalizer",
    "idna",
]

# Only meaningful in a --with-torch build. Listing these in a lite build would
# send PyInstaller hunting for modules that are deliberately excluded.
_HIDDEN_TORCH = [
    # The torch-based backends, also resolved by string -- see _HIDDEN_BASE.
    "livetranslate.asr.backends.funasr",
    "livetranslate.asr.backends.sensevoice",
    "livetranslate.asr.backends.funasr_nano",
    "livetranslate.asr.backends.anime_whisper",
    "livetranslate.asr.backends.whisper",
    # ASR backends and their model code
    "funasr",
    "funasr.models.sense_voice.model",
    "funasr.models.fun_asr_nano.model",
    "funasr.bin.inference",
    "funasr.utils.postprocess_utils",
    "ctranslate2",
    "ctranslate2.models",
    "ctranslate2.models.whisper",
    "ctranslate2.models.sense_voice",
    "faster_whisper",
    "faster_whisper.transcribe",
    "silero_vad",
    # torch / audio stack reached via torch.hub and torchaudio registries
    "torchaudio",
    "torchaudio._extension",
    "torch",
    "torch.hub",
    "torch.library",
    "torch.nn.functional",
    "soundfile",
    "librosa",
    # model download / cache backends
    "modelscope",
    "modelscope.hub.snapshot_download",
    "huggingface_hub",
    "transformers",
    "tokenizers",
    "safetensors",
    "safetensors.torch",
]

HIDDEN_IMPORTS = _HIDDEN_BASE + _YASBD_MODULES + ([] if LITE else _HIDDEN_TORCH)

_BASE_EXCLUDES = [
    # conda-forge base env baggage -- never imported by this app
    "conda", "anaconda", "IPython", "ipykernel", "jupyter", "notebook",
    "nbformat", "nbconvert", "matplotlib", "pandas", "scipy", "IPython.core",
    "tkinter", "pydoc_data",
    # NOTE: distutils must stay importable -- PyInstaller's own
    # hook-distutils aliases setuptools._distutils through it, and excluding
    # it fails the analysis with "already imported as ExcludedModule".
    # unittest must stay too: torch._dispatch.python imports it at module
    # level, so excluding it makes `import torch` die inside the frozen exe.
    # (In a lite build there is no torch, so unittest could go -- but keeping
    # it costs nothing and keeps the two flavours closer.)
    "pip", "wheel", "pkg_resources",
    # scikit-learn and friends: only pulled in by matplotlib/torch test paths
    "sklearn", "skimage", "networkx", "sympy", "numba", "llvmlite",
    # never used by the app or its deps at run time
    "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "PyQt6.QtQuick",
    "PyQt6.QtQml", "PyQt6.Qt3DCore", "PyQt6.QtBluetooth", "PyQt6.QtMultimedia",
    "PyQt6.QtWebSockets", "PyQt6.QtDesigner", "PyQt6.QtCharts",
    "PyQt6.QtDataVisualization", "PyQt6.QtPdf", "PyQt6.QtSql",
    "PyQt6.QtTest", "PyQt6.QtNetworkAuth", "PyQt6.QtPositioning",
    "PyQt6.QtSerialPort", "PyQt6.QtSensors", "PyQt6.QtSpatialAudio",
    # duplicative ASR stacks: ctranslate2 covers the whisper engines
    "openai_whisper", "whisper",
]

EXCLUDES = _BASE_EXCLUDES + (LITE_EXCLUDES if LITE else [])

# The frozen ASR worker is a multiprocessing spawn child; it must be able to
# re-import every module the parent already imported. PyInstaller ships the
# child entry point inside the archive, so nothing extra is needed here as
# long as main.py calls multiprocessing.freeze_support() (it does).
COLLECT_ALL = (
    ["sherpa_onnx", "yasbd"]
    if LITE
    else ["funasr", "ctranslate2", "silero_vad", "yasbd"]
)

SPEC_HEADER = '''# -*- mode: python ; coding: utf-8 -*-
# Generated by build_exe.py -- edit that script, not this file.

block_cipher = None

a = Analysis(
    [ENTRY],
    pathex=[PROJECT],
    binaries=BINARIES,
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    # yasbd discovers its sentence-boundary rules by listing
    # Path(__file__).parent with iterdir(), so its rules/ directory has to exist
    # on disk. Collected into the PYZ archive (the default) it does not, and the
    # frozen app dies inside the ASR loop with
    #     FileNotFoundError: .../<tmp>/yasbd/rules
    # which takes caption generation down with it. 'py' keeps the sources as
    # real files instead.
    module_collection_mode={'yasbd': 'py'},
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)
'''

SPEC_HEADER_ONEDIR = '''# -*- mode: python ; coding: utf-8 -*-
# Generated by build_exe.py -- edit that script, not this file.

block_cipher = None

a = Analysis(
    [ENTRY],
    pathex=[PROJECT],
    binaries=BINARIES,
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    # yasbd discovers its sentence-boundary rules by listing
    # Path(__file__).parent with iterdir(), so its rules/ directory has to exist
    # on disk. Collected into the PYZ archive (the default) it does not, and the
    # frozen app dies inside the ASR loop with
    #     FileNotFoundError: .../<tmp>/yasbd/rules
    # which takes caption generation down with it. 'py' keeps the sources as
    # real files instead.
    module_collection_mode={'yasbd': 'py'},
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
'''


def find_icon() -> str | None:
    for candidate in ("app.ico", "icon.ico", "screenshot/app.ico"):
        path = PROJECT_DIR / candidate
        if path.is_file():
            return str(path)
    return None


def build_spec() -> Path:
    spec_dir = WORK_DIR
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_path = spec_dir / f"{APP_NAME}{'-onedir' if ONEDIR else '-onefile'}.spec"

    def py_repr(value) -> str:
        return repr(str(value).replace("\\", "/"))

    def pair_repr(src, dst) -> str:
        return f"({src!r}, {dst!r})"

    data_lines = ",\n    ".join(
        pair_repr(str(src).replace("\\", "/"), dst)
        for src, dst in BUNDLED_DATA
        if Path(src).exists()
    )
    binary_lines = ",\n    ".join(
        pair_repr(src.replace("\\", "/"), dst) for src, dst in BINARIES
    )
    hidden_lines = ",\n    ".join(f'"{m}"' for m in HIDDEN_IMPORTS)
    exclude_lines = ",\n    ".join(f'"{m}"' for m in EXCLUDES)

    body = (
        (SPEC_HEADER_ONEDIR if ONEDIR else SPEC_HEADER)
        .replace("ENTRY", py_repr(PROJECT_DIR / "main.py"))
        .replace("PROJECT", py_repr(PROJECT_DIR))
        .replace("ICON", repr(find_icon()))
        .replace("APP_NAME", repr(APP_NAME))
        .replace(
            "BINARIES",
            "[\n    " + binary_lines + ",\n]" if binary_lines else "[]",
        )
        .replace(
            "DATAS",
            "[\n    " + data_lines + ",\n]" if data_lines else "[]",
        )
        .replace("HIDDEN_IMPORTS", "[\n    " + hidden_lines + ",\n]")
        .replace("EXCLUDES", "[\n    " + exclude_lines + ",\n]")
    )

    spec_path.write_text(body, encoding="utf-8")
    return spec_path


def main() -> int:
    print("=" * 68)
    print(f"  LiveTranslate { 'one-folder' if ONEDIR else 'one-file' } build")
    print("=" * 68)
    print(
        f"  flavour   : "
        f"{'lite (CPU-only, no torch)' if LITE else 'full (torch + CUDA)'}"
    )
    print(f"  entry     : main.py")
    print(f"  data      : {', '.join(d for _, d in BUNDLED_DATA)}")
    print(f"  binaries  : {len(BINARIES)} file(s)")
    print(f"  hidden    : {len(HIDDEN_IMPORTS)} module(s)")
    print(f"  excludes  : {len(EXCLUDES)} package(s)")
    print(f"  output    : {DIST_DIR / (APP_NAME if ONEDIR else APP_NAME + '.exe')}")
    print()

    # Deliberately NOT wiping dist/ or the work dir:
    # * the work dir holds PyInstaller's dependency-analysis cache, which is
    #   the expensive part (~7 min); wiping it makes every rebuild pay that
    #   again even when only a spec exclusion changed;
    # * mass-deleting a multi-GB tree is a good way to lose work to a
    #   mistyped path, and it trips bulk-delete guards.
    #
    # PyInstaller's --noconfirm *does* wipe its own output directory, so the
    # previous build is renamed out of the way first: a rename is atomic and
    # instant, and if this build fails the old one is still there to fall
    # back on. Pass --clean to drop the stash instead.
    target = DIST_DIR / (APP_NAME if ONEDIR else f"{APP_NAME}.exe")
    if target.exists():
        if "--clean" in sys.argv:
            import shutil as _shutil

            print("  --clean: removing previous output...")
            if target.is_dir():
                _shutil.rmtree(target)
            else:
                target.unlink()
        else:
            stamp = time.strftime("%H%M%S")
            stash = target.with_name(f"{target.name}.prev-{stamp}")
            print(f"  previous output -> {stash.name}")
            os.replace(target, stash)

    spec_path = build_spec()

    env = dict(os.environ)
    # A stale build cache silently produces a broken exe.
    env["PYTHONHASHSEED"] = "0"

    cmd = [
        sys.executable, "-m", "PyInstaller",
        str(spec_path),
        "--distpath", str(DIST_DIR),
        "--workpath", str(WORK_DIR / "work"),
        "--noconfirm",
        "--log-level", "WARN",
    ]
    print("  running:", " ".join(cmd[-8:]), "\n")

    result = subprocess.run(cmd, cwd=PROJECT_DIR, env=env)
    if result.returncode != 0:
        print("\n  BUILD FAILED")
        return result.returncode

    print("\n  BUILD OK")
    _report()
    if PACKAGE:
        _package_release()
    return 0


def _fmt_size(num_bytes: int) -> str:
    if num_bytes >= 1024**3:
        return f"{num_bytes / 1024 ** 3:.2f} GB"
    return f"{num_bytes / 1024 ** 2:.1f} MB"


def _package_release() -> None:
    """Zip the exe, its models and the shipped config into a release archive.

    The layout inside the zip is flat -- LiveTranslate.exe + models/ +
    config.yaml -- so releasing is "extract and double-click". The exe resolves
    APP_ROOT to its own directory, so the models are found with no configuration.
    """
    import zipfile

    models_dir = DIST_DIR / "models"
    exe = DIST_DIR / f"{APP_NAME}.exe"
    if not models_dir.is_dir():
        print("\n  package  : SKIPPED -- no models/ staged next to the exe")
        print("             (copy the ONNX models into dist/models/ first)")
        return

    files = [exe]
    for extra in ("config.yaml",):
        candidate = DIST_DIR / extra
        if candidate.is_file():
            files.append(candidate)
    files += [p for p in models_dir.rglob("*") if p.is_file()]

    stamp = time.strftime("%Y%m%d")
    archive = DIST_DIR / f"{APP_NAME}-{stamp}-win64.zip"
    print(f"\n  packaging: {len(files)} file(s) -> {archive.name}")
    # compresslevel 6: the ONNX weights barely compress, so going higher only
    # costs time. Stored paths are relative so the zip extracts flat.
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in files:
            zf.write(path, path.relative_to(DIST_DIR))
    print(f"  archive  : {_fmt_size(archive.stat().st_size)}  {archive}")


def _report() -> None:
    if ONEDIR:
        total = sum(
            f.stat().st_size
            for f in (DIST_DIR / APP_NAME).rglob("*")
            if f.is_file()
        )
        print(f"  folder size: {_fmt_size(total)}")
        print(f"  exe        : {DIST_DIR / APP_NAME / (APP_NAME + '.exe')}")
    else:
        exe = DIST_DIR / f"{APP_NAME}.exe"
        print(f"  exe size  : {_fmt_size(exe.stat().st_size)}")
        print(f"  exe       : {exe}")
    # The exe is only half the story: the ONNX models (~310MB) ship next to it.
    models_dir = DIST_DIR / "models"
    if models_dir.is_dir():
        total = sum(f.stat().st_size for f in models_dir.rglob("*") if f.is_file())
        print(f"  models    : {_fmt_size(total)}  (next to the exe)")
    else:
        print("  models    : not staged -- run build_release.ps1 to package them")


if __name__ == "__main__":
    if PACKAGE_ONLY:
        _package_release()
        sys.exit(0)
    sys.exit(main())
