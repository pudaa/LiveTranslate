# LiveTranslate

**English** | [中文](README_zh.md)

Real-time speech captions for Windows. Captures system audio (WASAPI loopback) and optional microphone input, recognises speech **on the CPU**, and displays the text in a transparent overlay. No subtitles needed on the video, no GPU needed on the machine, and no network needed unless you turn translation on.

Works with any system audio — videos, livestreams, voice chat. No player modifications needed.

> **This is a fork of [TheDeathDragon/LiveTranslate](https://github.com/TheDeathDragon/LiveTranslate).**
> Upstream translates foreign audio into your language in real time. This fork was repurposed for a different job: **watching videos that have no subtitles, silently** — where the caption itself is the product and translation is an optional layer on top. That shift drove a rebuilt subtitle renderer, per-application audio capture, a CPU-only inference path and a much smaller download. Details in [What this fork changes](#what-this-fork-changes).


![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue)
![Windows](https://img.shields.io/badge/Platform-Windows-0078d4)
![License](https://img.shields.io/badge/License-MIT-green)

## Screenshot

![LiveTranslate](screenshot/en.png)

## Video

[![Install & Demo](https://img.shields.io/badge/Bilibili-Install%20%26%20Demo-00A1D6?logo=bilibili)](https://www.bilibili.com/video/BV1K2Awz6Euw)

## Features

- **Real-time caption pipeline**: System audio → VAD (ONNX) → ASR (ONNX, on the CPU) → overlay. Translation optional
- **No GPU required**: the default engine is sherpa-onnx + SenseVoice, CPU-only
- **Captions first**: `captions` mode needs no API key and no network; `translate` mode keeps upstream's translation
- **Per-application capture**: record one process and leave every other window out
- **Multiple ASR engines**: sherpa-onnx (default, CPU), faster-whisper, SenseVoice, FunASR Nano, Anime-Whisper
- **Remote ASR**: offload speech recognition to a GPU machine over HTTP — see [REMOTE_ASR.md](REMOTE_ASR.md)
- **Any OpenAI-compatible API**: DeepSeek, Grok, Qwen, GPT, Ollama, vLLM, etc.
- **Streaming captions**: text appears as it is spoken, and partials replace themselves in place
- **Cinema presets**: 8 presets plus a pure mode, and SRT export
- **Microphone mix-in**: Optionally mix microphone input with system audio for ASR
- **Low-latency VAD**: 32ms chunks + Silero VAD (ONNX) with adaptive silence detection
- **Transparent overlay**: Always-on-top, click-through, draggable, 14 color themes
- **Auto model management**: Setup wizard, ModelScope / HuggingFace dual sources
- **Built-in benchmark**: Compare translation model speed and quality

## What this fork changes

Upstream and this fork share a pipeline but not a purpose, so day-to-day use is
quite different. 32 commits on top of upstream `190ef02`.

### 1. Captions are the product; translation is optional

`pipeline.mode` splits the two apart:

| Mode | What you get | API key needed? |
|---|---|---|
| `captions` | The recognised text **is** the subtitle | No — fully offline |
| `translate` | Upstream behaviour: original + translation | Yes |

In `captions` mode nothing leaves the machine. That is what this fork was built
for, and the reason a CPU-only build is viable at all.

### 2. The subtitle renderer was rebuilt

Upstream's overlay is fine for reading a finished translated line. Showing
*live* speech is a different problem:

- **Cinema presets** — 4 overlay + 4 standalone subtitle-window presets, plus a
  "pure mode" that hides all chrome. This was the original reason for the fork.
- **Streaming captions** — text appears as it is spoken, and a partial result
  replaces itself in place rather than being appended, so a sentence never shows
  up twice.
- **Paced release** — ASR delivers a whole segment at once, which makes it
  impossible to tell where the new part begins. Text is buffered and released one
  reading unit at a time (one CJK character, one Latin word).
- **Teleprompter scrolling** — the line's position is a free-running quantity
  rather than something derived from the text width, so **inserting text never
  moves text already on screen**. (The obvious alternative — measure the width
  and compensate — was tried and reverted three times: the compensation could
  not tell "visible content changed" from "invisible content changed".)
- **Corrections** — when the recogniser revises a word it gets a dotted underline
  (that marks *where*) and fades in from 45% (that marks *when*). A correction is
  deliberately not "new text appearing".
- **Stale fade** — 4s without new content fades the line out over 0.9s, so a long
  silence does not leave outdated text on screen.
- **SRT export** — cues are timed from the *captured audio clock*, not the wall
  clock, so a dropped frame or a paused capture cannot make the subtitles drift
  against the video.

### 3. Capture one application, not everything

Upstream records the whole output device. If you want captions for one video
while music plays in another window, that is not good enough. This fork uses
**WASAPI process loopback** (`AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK`) to
capture a single executable, and a settings page tells you whether isolation is
actually in effect. Requires Windows 10 2004+.

### 4. It runs on the CPU

The upstream build expects an NVIDIA GPU. The default engine here is
**sherpa-onnx** (ONNX Runtime), which needs no torch at all:

| | PyTorch + CUDA (upstream) | sherpa-onnx int8 (this fork) |
|---|---|---|
| 8s segment | ~95 ms | ~215 ms |
| Budget | 600 ms | 600 ms |
| Discrete GPU | **P0, ~21 W, 39% util** | **asleep, P8, ~2.7 W** |
| Runtime size | ~2.5 GB (torch + CUDA) | ~19 MB |

Both run the *same* SenseVoice-Small weights, so accuracy is unchanged — measured
on real lecture audio (`doc/02-端侧化与省电调研-2026-10-11.md`). Two notes:

- The VAD is ONNX as well, and was verified to produce **identical segmentation**
  to the torch version. Driving the whole capture path without torch is the point.
- sherpa-onnx's SenseVoice decode **drops punctuation** (the tokens are in the
  vocab; greedy int8 decoding loses them). A CT-Transformer model restores it for
  ~15 ms per segment. It can be switched off in settings.

### 5. Performance and packaging

- **FunASR import: 43s → 18s.** Upstream's `import funasr` eagerly walked all 432
  of its submodules, dragging in transformers and scipy for model families
  SenseVoice never touches.
- **CPU-only build.** `build_exe.py` produces a one-file exe (~200 MB) needing no
  torch and no CUDA; the ONNX models (~310 MB) sit beside it. The old
  PyTorch-based exe was 2.8 GB.
- **Checked by scripts, not by eye.** `tools/verify_ui.py` drives the real widgets
  offscreen and steps the subtitle animations frame by frame;
  `tools/verify_runtime.py` spawns a real ASR worker;
  `tools/bench_asr_backends.py` and `tools/compare_asr_quality.py` measure
  latency and accuracy on your own audio.

### Not changed

The pipeline shape, the translation client and the settings UI are upstream's.
Translation still works as before — `translate` mode can be turned back on at any
time.

## Changelog

See [English Changelog](i18n/CHANGELOG_en.md) | [中文更新日志](i18n/CHANGELOG_zh.md)

## Requirements

- **OS**: Windows 10/11 (per-application capture needs Windows 10 2004+)
- **GPU**: **not required** — the default engine runs on the CPU
- **Python**: 3.10–3.12 (only to run from source)
- **Network**: `captions` mode is fully offline; only `translate` mode needs a translation API

## Quick Start

### Release build (recommended)

Download the release archive, extract it, and double-click **`LiveTranslate.exe`**. The models ship inside the package, so the first launch needs no download, no network, and no Python or CUDA installation.

> Keep the directory layout when extracting — the `models/` folder next to `LiveTranslate.exe` is required.

### From source

```bash
git clone https://github.com/TheDeathDragon/LiveTranslate.git
cd LiveTranslate
```

Double-click **`install.bat`** — the installer will:
1. Detect Python 3.10–3.12 (auto-install via winget if missing)
2. Create a virtual environment
3. Auto-detect NVIDIA GPU and let you choose CUDA / CPU PyTorch
4. Install all dependencies

Then double-click **`start.bat`** to launch.

To update, double-click **`update.bat`** — it will pull the latest code and update dependencies (auto-installs Git via winget if missing).

<details>
<summary>Manual install</summary>

```bash
python -m venv .venv
.venv\Scripts\activate

# PyTorch (choose one)
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu126  # CUDA
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128  # CUDA (RTX 50xx)
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu    # CPU only

# Dependencies
pip install -r requirements.txt

# Launch
.venv\Scripts\python.exe main.py
```

</details>

## First Launch

The release build ships every model it needs, so the first launch is short:

1. A brief setup dialog appears — pick a download source and confirm
2. **Nothing is downloaded** — the ONNX models are already in `models/`
3. The main window appears

Running from source with no models present? The wizard fetches what the
*selected* engine needs. The default engine (sherpa-onnx) never downloads at
run time: its models are plain directories under `models/`, extracted from
GitHub Releases.

## Translation API

Settings → Translation tab:

| Parameter | Example |
|-----------|---------|
| API Base | `https://api.deepseek.com/v1` |
| API Key | Your key |
| Model | `deepseek-chat` |
| Proxy | `none` / `system` / custom URL |

## Architecture

```
Audio (WASAPI 32ms) → VAD (Silero, ONNX) → ASR (sherpa-onnx int8, CPU) → Overlay
         ↑ optional mic mix-in                        ↓
                                          optional: LLM translation
```

```
main.py                    Entry point & pipeline
livetranslate/             all code lives in the package
├── paths.py               Central path anchors (project root, models/, logs/, ...)
├── audio/
│   ├── capture.py         WASAPI loopback + mic mix-in + per-process capture
│   ├── vad.py             Silero VAD (ONNX Runtime; torch is optional fallback)
│   ├── sessions.py        Per-app session enumeration (which app is playing)
│   └── proc_loopback.py   WASAPI process loopback
├── asr/
│   ├── registry.py        Backend registry (lazy) + availability probe
│   ├── client.py          Main-process worker manager (spawn + Pipe IPC)
│   ├── worker.py          Subprocess entry point
│   ├── server.py          Standalone HTTP ASR server (remote-whisper)
│   ├── funasr_boot.py     Blocks 38 FunASR submodules SenseVoice never uses
│   └── backends/
│       ├── sherpa_onnx.py **Default.** SenseVoice int8 + punctuation (no torch)
│       ├── whisper.py     faster-whisper backend
│       ├── funasr.py      Unified FunASR model selector backend
│       ├── sensevoice.py  SenseVoice backend
│       ├── funasr_nano.py FunASR Nano backend
│       ├── anime_whisper.py Anime-Whisper backend (ja anime/galgame)
│       ├── remote.py      Remote Whisper client (see REMOTE_ASR.md)
│       └── nano_src/      Vendored FunASR-Nano code
├── translate/
│   └── translator.py      OpenAI-compatible client (streaming, JSON schema, context)
├── output/
│   ├── transcript.py      Per-session transcript writer
│   └── srt.py             SRT export (timed from the captured-audio clock)
├── ui/
│   ├── overlay.py         PyQt6 overlay
│   ├── subtitle_window.py Standalone subtitle window (OBS, outlined text)
│   ├── subtitle_settings.py Subtitle window settings UI
│   ├── control_panel.py   Settings UI (7 tabs)
│   ├── dialogs.py         Wizard, download & model config dialogs
│   ├── log_window.py      Real-time log viewer
│   └── i18n.py            UI strings
├── models/
│   └── manager.py         Model registry, download & cache
└── benchmark.py           Translation benchmark
```

`tools/` holds the verification and measurement scripts — they are the reason
the subtitle animations and the CPU path can be changed with any confidence.
See `tools/README.md`.

## Acknowledgements

- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) — ONNX inference engine (default ASR + VAD)
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — Whisper inference via CTranslate2
- [FunASR](https://github.com/modelscope/FunASR) — SenseVoice / Fun-ASR-Nano
- [Anime-Whisper](https://huggingface.co/litagin/anime-whisper) — Japanese anime/galgame ASR
- [Silero VAD](https://github.com/snakers4/silero-vad) — Voice activity detection

## Star History

<a href="https://www.star-history.com/?repos=TheDeathDragon%2FLiveTranslate&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=TheDeathDragon/LiveTranslate&type=date&theme=dark&legend=top-left&sealed_token=0t-kzcN9leqL-yaHdKcBdHLdLlE6NNHa48RpsLvUM3u2fOiZOYqWfjgplXxAtjk1ZJciSAbzI3gZ4PQqqHrOv4abM1CpOomUymVX6J1zPN-3Ygu0-Xr8Kpj3Xt8jWS05B4tTpuNSmoYqyHipPvKC7lxGfLcOF_zctjqvOka-j9gYWct0oQyJnGjdcZxY" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=TheDeathDragon/LiveTranslate&type=date&legend=top-left&sealed_token=0t-kzcN9leqL-yaHdKcBdHLdLlE6NNHa48RpsLvUM3u2fOiZOYqWfjgplXxAtjk1ZJciSAbzI3gZ4PQqqHrOv4abM1CpOomUymVX6J1zPN-3Ygu0-Xr8Kpj3Xt8jWS05B4tTpuNSmoYqyHipPvKC7lxGfLcOF_zctjqvOka-j9gYWct0oQyJnGjdcZxY" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=TheDeathDragon/LiveTranslate&type=date&legend=top-left&sealed_token=0t-kzcN9leqL-yaHdKcBdHLdLlE6NNHa48RpsLvUM3u2fOiZOYqWfjgplXxAtjk1ZJciSAbzI3gZ4PQqqHrOv4abM1CpOomUymVX6J1zPN-3Ygu0-Xr8Kpj3Xt8jWS05B4tTpuNSmoYqyHipPvKC7lxGfLcOF_zctjqvOka-j9gYWct0oQyJnGjdcZxY" />
 </picture>
</a>

## License

[MIT License](LICENSE)
