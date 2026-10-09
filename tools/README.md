# tools/

Development and verification scripts. **None of these ship in a release** —
`build_release.ps1` drops this whole directory, and `start.bat` is the only
entry point you normally need.

Run everything through the launcher rather than by hand, so the right
interpreter is used:

```cmd
start.bat verify     imports + runtime + UI + pytest
start.bat gpu        verify ASR really loads and runs on the GPU
start.bat audio      end-to-end audio-source checks (plays a quiet tone)
```

## Verification

| Script | What it proves |
|---|---|
| `verify_imports.py` | Every package-internal import resolves, statically, without the dependencies installed. Catches a bad move or rename. |
| `verify_runtime.py` | The things only a live interpreter shows: all modules import, path anchors point at the project root, the ASR registry resolves *without* pulling torch into the process, an ASR worker survives a real `spawn` round-trip, and `captions` mode never submits to the translator while still emitting SRT cues. |
| `verify_ui.py` | Offscreen Qt: control-panel and subtitle-window cinema presets apply end to end, the overlay's pure mode and captions-only rendering, and the audio-source group builds lazily then refreshes. |
| `verify_gpu_asr.py` | Loads SenseVoice on the real GPU and transcribes once. Only a real device run catches a bad torch build, a driver mismatch or missing cudnn — those are invisible until the model touches the device. |
| `verify_process_loopback.py` | Process loopback captures the target process (non-silent) **and excludes an unrelated one (silent)**. The second half is the point: non-silence alone only shows "something was captured". |
| `verify_audio_switch.py` | `AudioCapture` really follows its target: scoped to a tone player it delivers audio, switched to a silent process it delivers silence, and switching back restores device-wide capture. |

## Setup / maintenance

| Script | Purpose |
|---|---|
| `prefetch_models.py` | Download the Silero VAD + ASR models without going through the GUI wizard, so an install can be prepared (or checked) headlessly. |
| `check_loopback_audio.py` | Manual left-over from upstream: captures loopback for a moment and prints whether the data is non-zero. Useful when you suspect the output device is the problem. |

## Notes for anyone extending these

* **Integration checks must use uniquely-named executables.** `verify_audio_switch.py`
  copies `python.exe` to `lt_tone_probe.exe` rather than targeting `python.exe`,
  because that name also matches the test process itself and any other Python on
  the machine, leaving `find_pids_by_name` to guess.
* **Never name a file here `test_*.py`.** pytest would import it during
  collection, and `check_loopback_audio.py` opens audio devices at module level —
  which is exactly why it is not called `test_audio.py` any more.
* Scripts resolve the project root from `Path(__file__).resolve().parents[1]`,
  so they work from any working directory.
