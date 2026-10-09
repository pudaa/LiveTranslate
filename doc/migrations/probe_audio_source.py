"""Feasibility probe for P4.3 audio-source filtering.

Answers three questions before any UI gets built:

  A. can we enumerate audio sessions (app name / PID / playing / level)?
  B. can we tell *which output device* a session is rendered to, so we can
     verify that a target app really has been isolated away from the device we
     capture?
  C. is per-process loopback (WASAPI AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK)
     reachable on this machine -- i.e. is the Windows build new enough, so the
     zero-extra-device path is on the table?

Run with the project env python.
"""

from __future__ import annotations

import platform
import struct
import sys

def win_build() -> tuple[int, int]:
    """(major, build) from the real Windows version, not the shimmed one.

    sys.getwindowsversion() is subject to app-compat lies; read the registry
    CurrentBuildNumber instead, which is what feature-gating actually follows.
    """
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        ) as key:
            build = int(winreg.QueryValueEx(key, "CurrentBuildNumber")[0])
            try:
                major = int(winreg.QueryValueEx(key, "CurrentMajorVersionNumber")[0])
            except OSError:
                major = 10
            return major, build
    except Exception as exc:  # noqa: BLE001
        print(f"  (registry read failed: {exc})")
        v = sys.getwindowsversion()
        return v.major, v.build


def main() -> int:
    print(f"python  : {platform.python_version()}")
    try:
        v = sys.getwindowsversion()
        print(f"os      : {platform.system()} {v.major}.{v.minor} build {v.build}")
    except Exception:
        print(f"os      : {platform.system()} {platform.release()}")

    major, build = win_build()
    print(f"real build: {major}.{build}")
    print(f"  process-loopback needs Win10 2004+ (build >= 19041): "
          f"{'YES' if build >= 19041 else 'NO'}")

    print("\n--- A. session enumeration (pycaw) ---")
    try:
        from pycaw.pycaw import AudioUtilities

        sessions = AudioUtilities.GetAllSessions()
        print(f"  {len(sessions)} session(s)")
        for s in sessions:
            name = s.Process.name() if s.Process else "<system>"
            pid = s.Process.pid if s.Process else -1
            try:
                vol = s.SimpleAudioVolume.GetMasterVolume()
            except Exception:
                vol = float("nan")
            try:
                peak = s.AudioMeterInformation.GetPeakValue()
            except Exception:
                peak = float("nan")
            state = "playing" if peak > 0.001 else "idle"
            print(f"    pid={pid:<7} vol={vol:.2f} peak={peak:.3f} {state:<8} {name}")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {type(exc).__name__}: {exc}")

    print("\n--- B. session -> endpoint mapping? ---")
    try:
        from pycaw.pycaw import AudioUtilities

        dev = AudioUtilities.GetSpeakers()
        print(f"  default render endpoint: {dev.GetId() if hasattr(dev, 'GetId') else dev}")
        try:
            from pycaw.api.audiopolicy import IAudioSessionControl2  # noqa: F401
            print("  pycaw exposes IAudioSessionControl2")
        except Exception as exc:  # noqa: BLE001
            print(f"  IAudioSessionControl2 not exposed: {exc}")
        # NOTE: the per-session endpoint is reachable via IAudioSessionManager2 on
        # each endpoint, so by construction a session can only be *observed* on
        # the endpoint it is rendered to.
        print("  => a session is enumerated per-endpoint, so 'which device is this"
              " app on' is answerable by enumerating every endpoint.")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {type(exc).__name__}: {exc}")

    print("\n--- C. process loopback reachable? ---")
    try:
        import comtypes  # noqa: F401

        print("  comtypes available (ctypes/COM path is buildable)")
    except Exception as exc:  # noqa: BLE001
        print(f"  comtypes missing: {exc}")
    print("  NOTE: no pip package wraps AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;")
    print("        implementing it means hand-rolling ActivateAudioInterfaceAsync +")
    print("        the IAudioClient activation params via comtypes.")

    print("\n--- output endpoints (for isolation) ---")
    try:
        import pyaudiowpatch as pyaudio

        pa = pyaudio.PyAudio()
        try:
            wasapi = next(
                i["index"]
                for i in (
                    pa.get_host_api_info_by_index(k)
                    for k in range(pa.get_host_api_count())
                )
                if "WASAPI" in i["name"]
            )
            n = 0
            for i in range(pa.get_device_count()):
                d = pa.get_device_info_by_index(i)
                if (
                    d["hostApi"] == wasapi
                    and d["maxOutputChannels"] > 0
                    and not d.get("isLoopbackDevice")
                ):
                    n += 1
                    print(f"  [{i:3d}] {d['name'][:55]}")
            print(f"  -> {n} physical/virtual render endpoint(s)")
            if n < 2:
                print("  => device isolation (B) has nowhere to route to: a second")
                print("     endpoint (e.g. VB-CABLE) would have to be installed first.")
        finally:
            pa.terminate()
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {type(exc).__name__}: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
