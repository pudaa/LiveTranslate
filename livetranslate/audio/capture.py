import logging
import threading
import queue
import time
import numpy as np
import pyaudiowpatch as pyaudio

log = logging.getLogger("LiveTranslate.Audio")

DEVICE_CHECK_INTERVAL = 2.0  # seconds

# Ceiling for the resampled mic buffer, in seconds of audio.
#
# Nothing drains this buffer while the loopback stream is down: a disconnected
# output device leaves `loopback_audio is None`, the read loop takes the
# `continue` early-exit, and the mic keeps appending. Without a cap it grows for
# the entire outage -- and since every append was an np.concatenate, the cost
# also degraded to quadratic as it grew.
MIC_BUFFER_MAX_SECONDS = 2.0

# Same idea for the per-process source, which delivers whatever the OS has
# buffered rather than a fixed chunk size.
PROC_BUFFER_MAX_SECONDS = 2.0

# How long to wait before retrying a failed per-process capture. The target
# application may have exited; if it comes back we want to reconnect on our own.
PROC_RETRY_INTERVAL = 3.0


def list_output_devices():
    """Return list of WASAPI output device names."""
    pa = pyaudio.PyAudio()
    devices = []
    wasapi_idx = None
    for i in range(pa.get_host_api_count()):
        info = pa.get_host_api_info_by_index(i)
        if "WASAPI" in info["name"]:
            wasapi_idx = info["index"]
            break
    if wasapi_idx is not None:
        for i in range(pa.get_device_count()):
            dev = pa.get_device_info_by_index(i)
            if (
                dev["hostApi"] == wasapi_idx
                and dev["maxOutputChannels"] > 0
                and not dev.get("isLoopbackDevice", False)
            ):
                devices.append(dev["name"])
    pa.terminate()
    return devices


def list_input_devices():
    """Return list of WASAPI input (microphone) device names."""
    pa = pyaudio.PyAudio()
    devices = []
    wasapi_idx = None
    for i in range(pa.get_host_api_count()):
        info = pa.get_host_api_info_by_index(i)
        if "WASAPI" in info["name"]:
            wasapi_idx = info["index"]
            break
    if wasapi_idx is not None:
        for i in range(pa.get_device_count()):
            dev = pa.get_device_info_by_index(i)
            if (
                dev["hostApi"] == wasapi_idx
                and dev["maxInputChannels"] > 0
                and not dev.get("isLoopbackDevice", False)
            ):
                devices.append(dev["name"])
    pa.terminate()
    return devices


class AudioCapture:
    """Capture system audio via WASAPI loopback using pyaudiowpatch."""

    def __init__(self, device=None, sample_rate=16000, chunk_duration=0.5):
        self.sample_rate = sample_rate
        self.chunk_duration = chunk_duration
        self.audio_queue = queue.Queue(maxsize=100)
        self._stream = None
        self._running = False
        self._device_name = device
        self._pa = pyaudio.PyAudio()
        self._read_thread = None
        self._native_channels = 2
        self._native_rate = 44100
        self._current_device_name = None
        self._loopback_disabled = False
        self._lock = threading.Lock()
        self._restart_event = threading.Event()
        # Microphone input
        self._mic_device_name = None
        self._mic_stream = None
        self._mic_native_rate = 44100
        self._mic_native_channels = 1
        self._mic_restart_event = threading.Event()
        self._mic_buf = np.array([], dtype=np.float32)

        # Per-process source (see set_target_process). When _target_process is
        # set the loop reads from a process-loopback capture instead of the
        # device-wide PyAudio stream.
        self._target_process: str | None = None
        self._pending_target_process: str | None = None
        self._process_capture = None
        self._process_pid: int | None = None
        self._proc_buf = np.array([], dtype=np.float32)
        self._proc_last_attempt = 0.0
        self._proc_restart_event = threading.Event()

    def _get_wasapi_info(self):
        for i in range(self._pa.get_host_api_count()):
            info = self._pa.get_host_api_info_by_index(i)
            if "WASAPI" in info["name"]:
                return info
        return None

    def _get_default_output_name(self):
        wasapi_info = self._get_wasapi_info()
        if wasapi_info is None:
            return None
        default_idx = wasapi_info["defaultOutputDevice"]
        default_dev = self._pa.get_device_info_by_index(default_idx)
        return default_dev["name"]

    @staticmethod
    def _query_current_default():
        """Create a fresh PA instance to get the actual current default device."""
        pa = pyaudio.PyAudio()
        try:
            for i in range(pa.get_host_api_count()):
                info = pa.get_host_api_info_by_index(i)
                if "WASAPI" in info["name"]:
                    default_idx = info["defaultOutputDevice"]
                    dev = pa.get_device_info_by_index(default_idx)
                    return dev["name"]
        finally:
            pa.terminate()
        return None

    def _find_loopback_device(self):
        """Find WASAPI loopback device for the default output."""
        wasapi_info = self._get_wasapi_info()
        if wasapi_info is None:
            raise RuntimeError("WASAPI host API not found")

        default_output_idx = wasapi_info["defaultOutputDevice"]
        default_output = self._pa.get_device_info_by_index(default_output_idx)
        log.info(f"Default output: {default_output['name']}")

        target_name = self._device_name or default_output["name"]

        for i in range(self._pa.get_device_count()):
            dev = self._pa.get_device_info_by_index(i)
            if dev["hostApi"] == wasapi_info["index"] and dev.get(
                "isLoopbackDevice", False
            ):
                if target_name in dev["name"]:
                    return dev

        # Fallback: any loopback device
        for i in range(self._pa.get_device_count()):
            dev = self._pa.get_device_info_by_index(i)
            if dev.get("isLoopbackDevice", False):
                return dev

        raise RuntimeError("No WASAPI loopback device found")

    def _open_stream(self):
        """Open stream for current default loopback device."""
        loopback_dev = self._find_loopback_device()
        self._native_channels = loopback_dev["maxInputChannels"]
        self._native_rate = int(loopback_dev["defaultSampleRate"])
        self._current_device_name = loopback_dev["name"]

        log.info(f"Loopback device: {loopback_dev['name']}")
        log.info(
            f"Native: {self._native_rate}Hz, {self._native_channels}ch -> {self.sample_rate}Hz mono"
        )

        native_chunk = int(self._native_rate * self.chunk_duration)

        self._stream = self._pa.open(
            format=pyaudio.paFloat32,
            channels=self._native_channels,
            rate=self._native_rate,
            input=True,
            input_device_index=loopback_dev["index"],
            frames_per_buffer=native_chunk,
        )

    def _close_stream(self):
        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def _find_mic_device(self):
        """Find WASAPI input device matching _mic_device_name, or system default."""
        wasapi_info = self._get_wasapi_info()
        if wasapi_info is None:
            raise RuntimeError("WASAPI host API not found")
        if self._mic_device_name in ("__default__", "default"):
            default_idx = wasapi_info["defaultInputDevice"]
            dev = self._pa.get_device_info_by_index(default_idx)
            if dev["maxInputChannels"] > 0:
                return dev
            raise RuntimeError("Default input device has no input channels")
        for i in range(self._pa.get_device_count()):
            dev = self._pa.get_device_info_by_index(i)
            if (
                dev["hostApi"] == wasapi_info["index"]
                and dev["maxInputChannels"] > 0
                and not dev.get("isLoopbackDevice", False)
                and dev["name"] == self._mic_device_name
            ):
                return dev
        raise RuntimeError(f"Mic device not found: {self._mic_device_name}")

    def _open_mic_stream(self):
        """Open microphone input stream."""
        dev = self._find_mic_device()
        self._mic_native_channels = dev["maxInputChannels"]
        self._mic_native_rate = int(dev["defaultSampleRate"])
        native_chunk = int(self._mic_native_rate * self.chunk_duration)
        log.info(
            f"Mic device: {dev['name']} ({self._mic_native_rate}Hz, {self._mic_native_channels}ch)"
        )
        self._mic_stream = self._pa.open(
            format=pyaudio.paFloat32,
            channels=self._mic_native_channels,
            rate=self._mic_native_rate,
            input=True,
            input_device_index=dev["index"],
            frames_per_buffer=native_chunk,
        )

    def _close_mic_stream(self):
        if self._mic_stream:
            try:
                self._mic_stream.stop_stream()
                self._mic_stream.close()
            except Exception:
                pass
            self._mic_stream = None

    def set_mic_device(self, device_name):
        """Change mic device at runtime. None = disabled."""
        if device_name == self._mic_device_name:
            return
        log.info(f"Mic device changed: {self._mic_device_name} -> {device_name}")
        self._mic_device_name = device_name
        if self._running:
            self._mic_restart_event.set()

    def set_device(self, device_name):
        """Change capture device at runtime. None = system default, '__disabled__' = off."""
        if device_name == self._device_name:
            return
        log.info(f"Audio device changed: {self._device_name} -> {device_name}")
        self._device_name = device_name
        self._loopback_disabled = device_name == "__disabled__"
        if self._running:
            self._restart_event.set()

    def _resample_to_mono(self, data, native_channels, native_rate):
        """Convert raw bytes to mono float32 at self.sample_rate."""
        audio = np.frombuffer(data, dtype=np.float32)
        if native_channels > 1:
            audio = audio.reshape(-1, native_channels).mean(axis=1)
        if native_rate != self.sample_rate:
            ratio = self.sample_rate / native_rate
            n_out = int(len(audio) * ratio)
            indices = np.arange(n_out) / ratio
            indices = np.clip(indices, 0, len(audio) - 1)
            idx_floor = indices.astype(np.int64)
            idx_ceil = np.minimum(idx_floor + 1, len(audio) - 1)
            frac = (indices - idx_floor).astype(np.float32)
            audio = audio[idx_floor] * (1 - frac) + audio[idx_ceil] * frac
        return audio

    def _mic_buffer_push(self, chunk: np.ndarray) -> None:
        """Append resampled mic audio, keeping the buffer under the cap.

        Drops the *oldest* audio on overflow: the newest samples are the ones
        that line up with the loopback chunk they are about to be mixed into.
        """
        self._mic_buf = np.concatenate([self._mic_buf, chunk])
        overflow = self._mic_buf.size - int(self.sample_rate * MIC_BUFFER_MAX_SECONDS)
        if overflow > 0:
            self._mic_buf = self._mic_buf[overflow:]
            log.debug(
                f"Mic buffer overrun, dropped {overflow / self.sample_rate:.2f}s "
                "(loopback stream is likely down)"
            )

    # ── per-process source ──────────────────────────────────────────────────

    @property
    def _wants_process_source(self) -> bool:
        """True when a per-process target is configured (or about to be).

        ``_pending_target_process`` matters because main.py calls
        set_target_process before start(), and the read loop only promotes it to
        ``_target_process`` on its first pass.
        """
        return (self._pending_target_process or self._target_process) is not None

    def set_target_process(self, name: str | None) -> None:
        """Capture one application instead of the whole output device.

        ``name`` is an executable name such as ``"vlc.exe"``; ``None`` restores
        device-wide capture. The reopen is deferred to the read thread, which
        owns the stream -- doing it here would race with it.
        """
        name = (name or "").strip() or None
        if name == self._pending_target_process:
            return
        self._pending_target_process = name
        self._proc_restart_event.set()

    @property
    def target_process(self) -> str | None:
        return self._target_process

    def _open_process_capture(self) -> bool:
        """Resolve the target executable and open a loopback capture for it."""
        from livetranslate.audio import proc_loopback as plp

        if not plp.available():
            log.error("Process loopback is unavailable on this Windows build")
            return False
        if not self._target_process:
            return False

        pids = plp.find_pids_by_name(self._target_process)
        if not pids:
            log.warning(f"Target process not running: {self._target_process}")
            return False
        # Prefer the pid we already had if it is still alive; otherwise take the
        # highest, which is the best available guess at "the most recent
        # instance" of that executable.
        pid = self._process_pid if self._process_pid in pids else max(pids)

        try:
            capture = plp.ProcessLoopbackCapture(pid)
            capture.open()
        except Exception as exc:  # noqa: BLE001
            log.error(
                f"Process loopback open failed for '{self._target_process}' "
                f"(pid {pid}): {exc}"
            )
            return False

        self._process_capture = capture
        self._process_pid = pid
        self._proc_buf = np.array([], dtype=np.float32)
        log.info(
            f"Audio source: process '{self._target_process}' (pid {pid}) -- "
            "other applications are not captured"
        )
        return True

    def _close_process_capture(self) -> None:
        cap, self._process_capture = self._process_capture, None
        self._process_pid = None
        if cap is not None:
            try:
                cap.close()
            except Exception:  # noqa: BLE001
                pass

    def _read_process_chunk(self) -> np.ndarray | None:
        """One fixed-size chunk of 16 kHz mono float32, or None if not enough is
        buffered yet.

        The OS hands over however much it has rather than a fixed chunk size, so
        re-chunk here to match the endpoint path's contract.
        """
        cap = self._process_capture
        if cap is None:
            # Not open: the target may have exited, or the first attempt failed.
            # Retry slowly so a restarted application reconnects on its own.
            now = time.monotonic()
            if now - self._proc_last_attempt >= PROC_RETRY_INTERVAL:
                self._proc_last_attempt = now
                self._open_process_capture()
            return None

        try:
            raw = cap.read()
        except Exception as exc:  # noqa: BLE001
            log.warning(f"Process loopback read failed, will retry: {exc}")
            self._close_process_capture()
            self._proc_last_attempt = time.monotonic()
            return None

        if raw:
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            if samples.size:
                self._proc_buf = np.concatenate([self._proc_buf, samples])
                cap_samples = int(self.sample_rate * PROC_BUFFER_MAX_SECONDS)
                if self._proc_buf.size > cap_samples:
                    self._proc_buf = self._proc_buf[-cap_samples:]

        need = int(self.sample_rate * self.chunk_duration)
        if self._proc_buf.size < need:
            return None
        chunk = self._proc_buf[:need]
        self._proc_buf = self._proc_buf[need:]
        return chunk

    def _restart_stream(self):
        """Restart stream with new default device."""
        with self._lock:
            self._close_stream()
            self._pa.terminate()
            self._pa = pyaudio.PyAudio()
            if self._loopback_disabled or self._wants_process_source:
                log.info("Loopback stream not needed for the current audio source")
            else:
                self._open_stream()
            # Re-open mic if active
            if self._mic_device_name:
                self._close_mic_stream()
                try:
                    self._open_mic_stream()
                except Exception as e:
                    log.warning(f"Failed to re-open mic after restart: {e}")

    def _read_loop(self):
        """Synchronous read loop in a background thread."""
        last_device_check = time.monotonic()

        while self._running:
            # Handle pending loopback restart request
            if self._restart_event.is_set():
                self._restart_event.clear()
                try:
                    self._restart_stream()
                    while not self.audio_queue.empty():
                        try:
                            self.audio_queue.get_nowait()
                        except queue.Empty:
                            break
                    log.info(f"Audio capture restarted on: {self._current_device_name}")
                except Exception as e:
                    log.error(f"Restart after device change failed: {e}")
                    time.sleep(0.5)
                continue

            # Handle mic device change
            if self._mic_restart_event.is_set():
                self._mic_restart_event.clear()
                self._close_mic_stream()
                self._mic_buf = np.array([], dtype=np.float32)
                if self._mic_device_name:
                    try:
                        self._open_mic_stream()
                        log.info(f"Mic stream opened: {self._mic_device_name}")
                    except Exception as e:
                        log.error(f"Failed to open mic: {e}")
                else:
                    log.info("Mic disabled")

            # Auto-switch only when using system default
            now = time.monotonic()
            if now - last_device_check > DEVICE_CHECK_INTERVAL:
                last_device_check = now
                if self._device_name is None:
                    try:
                        current_default = self._query_current_default()
                        if (
                            current_default
                            and self._current_device_name
                            and current_default not in self._current_device_name
                        ):
                            log.info(
                                f"System default output changed: "
                                f"{self._current_device_name} -> {current_default}"
                            )
                            log.info("Restarting audio capture for new device...")
                            self._restart_stream()
                            log.info(
                                f"Audio capture restarted on: {self._current_device_name}"
                            )
                    except Exception as e:
                        log.warning(f"Device check error: {e}")

            # Handle switching between the device-wide source and a per-process
            # one. Reopening here rather than in set_target_process keeps every
            # touch of the source on this thread.
            if self._proc_restart_event.is_set():
                self._proc_restart_event.clear()
                self._close_process_capture()
                self._target_process = self._pending_target_process
                if self._target_process:
                    self._open_process_capture()
                else:
                    log.info("Audio source: output device (all applications)")
                # Drop anything queued from the previous source so the two are
                # never spliced into the same utterance.
                while not self.audio_queue.empty():
                    try:
                        self.audio_queue.get_nowait()
                    except queue.Empty:
                        break

            # Read one chunk from whichever source is configured.
            #
            #  * endpoint (default): WASAPI loopback of an output device, i.e.
            #    *everything* playing on it.
            #  * process: process loopback scoped to one executable, so other
            #    applications are not captured at all -- and unlike routing the
            #    target to a second output device, this needs no extra hardware.
            #
            # Both paths produce 16 kHz mono float32, so the mic mixing below is
            # shared and needs no special casing.
            loopback_audio = None
            if self._target_process is not None:
                loopback_audio = self._read_process_chunk()
                if loopback_audio is None:
                    time.sleep(0.005)
                    continue
            elif self._loopback_disabled:
                time.sleep(self.chunk_duration)
                n_samples = int(self.sample_rate * self.chunk_duration)
                loopback_audio = np.zeros(n_samples, dtype=np.float32)
            else:
                native_chunk = int(self._native_rate * self.chunk_duration)
                try:
                    data = None
                    with self._lock:
                        if not self._stream:
                            time.sleep(0.005)
                            continue
                        if self._stream.get_read_available() >= native_chunk:
                            data = self._stream.read(
                                native_chunk, exception_on_overflow=False
                            )
                    if data is not None:
                        loopback_audio = self._resample_to_mono(
                            data, self._native_channels, self._native_rate
                        )
                except Exception as e:
                    if self._restart_event.is_set():
                        continue
                    log.warning(f"Read error (device may have changed): {e}")
                    try:
                        time.sleep(0.5)
                        self._restart_stream()
                        log.info("Stream restarted after read error")
                    except Exception as re:
                        log.error(f"Restart failed: {re}")
                        time.sleep(1)
                    continue

            # Drain all available mic data into buffer
            if self._mic_stream:
                try:
                    avail = self._mic_stream.get_read_available()
                    if avail > 0:
                        mic_data = self._mic_stream.read(
                            avail, exception_on_overflow=False
                        )
                        mic_16k = self._resample_to_mono(
                            mic_data, self._mic_native_channels, self._mic_native_rate
                        )
                        self._mic_buffer_push(mic_16k)
                except Exception as e:
                    log.warning(f"Mic read error: {e}")

            if loopback_audio is None:
                time.sleep(0.005)
                continue

            # Mix: take matching length from mic buffer
            audio = loopback_audio
            mic_rms = None
            if len(self._mic_buf) > 0:
                n = len(loopback_audio)
                if len(self._mic_buf) >= n:
                    mic_chunk = self._mic_buf[:n]
                    self._mic_buf = self._mic_buf[n:]
                else:
                    mic_chunk = np.zeros(n, dtype=np.float32)
                    mic_chunk[: len(self._mic_buf)] = self._mic_buf
                    self._mic_buf = np.array([], dtype=np.float32)
                mic_rms = float(np.sqrt(np.mean(mic_chunk**2)))
                audio = loopback_audio + mic_chunk

            try:
                self.audio_queue.put_nowait((audio, mic_rms))
            except queue.Full:
                self.audio_queue.get_nowait()
                self.audio_queue.put_nowait((audio, mic_rms))

    def start(self):
        self._loopback_disabled = self._device_name == "__disabled__"
        if self._wants_process_source:
            # The device-wide stream is never read when scoped to a process, so
            # do not open it at all.
            log.info(
                "Audio source: process "
                f"'{self._pending_target_process or self._target_process}' "
                "(device stream not opened)"
            )
        elif not self._loopback_disabled:
            self._open_stream()
        else:
            log.info("Loopback disabled (mic-only mode)")
        if self._mic_device_name:
            try:
                self._open_mic_stream()
            except Exception as e:
                log.warning(f"Failed to open mic on start: {e}")
        self._running = True
        self._read_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._read_thread.start()
        log.info("Audio capture started")

    def stop(self):
        self._running = False
        if self._read_thread:
            self._read_thread.join(timeout=3)
        self._close_stream()
        self._close_mic_stream()
        self._close_process_capture()
        log.info("Audio capture stopped")

    def get_audio(self, timeout=1.0):
        try:
            return self.audio_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def __del__(self):
        # getattr: __del__ can run when __init__ never completed (or during
        # interpreter shutdown, after module globals are torn down), and an
        # exception here is swallowed by Python into a noisy unraisable warning.
        pa = getattr(self, "_pa", None)
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                pass
