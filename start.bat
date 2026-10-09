@echo off
REM ============================================================================
REM  LiveTranslate - launcher
REM
REM  Still called start.bat, but this fork runs on an isolated conda environment
REM  rather than the project .venv that install.ps1 builds -- so it picks
REM  whichever environment actually exists instead of hard-requiring .venv.
REM
REM  Usage:
REM    start.bat            launch the app
REM    start.bat verify     run the self-checks (imports, runtime, UI, pytest)
REM    start.bat gpu        check that ASR really loads and runs on the GPU
REM    start.bat audio      end-to-end audio-source checks (plays a quiet tone)
REM    start.bat help       this text
REM
REM  Written with goto-based flow and no nested ( ) blocks: a parenthesis inside
REM  an echoed string within an if-block closes the block early and cmd then
REM  tries to run the rest as a command.
REM ============================================================================
setlocal
cd /d "%~dp0"
set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"

set "LOGDIR=%~dp0logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

REM -- pick an interpreter: the conda env first, then upstream's .venv ---------
set "PY="
set "PY_KIND="
set "CONDA_PY=D:\DevVmEnv\Anaconda_envs\envs\livetranslate\python.exe"

if exist "%CONDA_PY%" set "PY=%CONDA_PY%"
if exist "%CONDA_PY%" set "PY_KIND=conda env livetranslate"

if not defined PY if not exist ".venv\Scripts\python.exe" goto no_python
if not defined PY if not exist ".venv\.livetranslate-ready" goto venv_incomplete
if not defined PY set "PY=.venv\Scripts\python.exe"
if not defined PY set "PY_KIND=.venv"

if /i "%~1"=="help" goto usage
if /i "%~1"=="-h" goto usage
if /i "%~1"=="--help" goto usage
if /i "%~1"=="verify" goto do_verify
if /i "%~1"=="gpu" goto do_gpu
if /i "%~1"=="audio" goto do_audio

echo ============================================================
echo  LiveTranslate
echo  python : %PY%
echo  source : %PY_KIND%
echo ============================================================
echo.
echo Starting... close this window or press Ctrl+C to stop
echo Full log: %LOGDIR%\app_start.log
echo.

REM Tee stdout+stderr to a log so a crash that kills the window is still
REM diagnosable afterwards.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "& '%PY%' '%~dp0main.py' 2>&1 | Tee-Object -FilePath '%LOGDIR%\app_start.log'"

set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" goto app_failed
echo LiveTranslate exited normally.
endlocal
exit /b 0

:app_failed
echo [ERROR] LiveTranslate exited with code %RC%
echo         See %LOGDIR%\app_start.log
echo.
pause
endlocal
exit /b %RC%

REM -- modes ------------------------------------------------------------------

:do_verify
echo Running self-checks using %PY_KIND%
echo.
"%PY%" "%~dp0tools\verify_imports.py"
echo.
"%PY%" "%~dp0tools\verify_runtime.py"
echo.
"%PY%" "%~dp0tools\verify_ui.py"
echo.
"%PY%" -m pytest "%~dp0tests" -q
echo.
pause
endlocal
exit /b 0

:do_gpu
echo Checking that the ASR backend loads and runs on the GPU...
echo.
"%PY%" "%~dp0tools\verify_gpu_asr.py"
echo.
pause
endlocal
exit /b 0

:do_audio
echo End-to-end audio-source checks. These play a short, quiet tone.
echo.
"%PY%" "%~dp0tools\verify_process_loopback.py"
echo.
"%PY%" "%~dp0tools\verify_audio_switch.py"
echo.
pause
endlocal
exit /b 0

:usage
echo LiveTranslate launcher
echo.
echo   start.bat            launch the app
echo   start.bat verify     run the self-checks: imports, runtime, UI, pytest
echo   start.bat gpu        check that ASR really loads and runs on the GPU
echo   start.bat audio      end-to-end audio-source checks, plays a quiet tone
echo   start.bat help       this text
echo.
echo Interpreter selected: %PY%
echo Kind: %PY_KIND%
echo.
pause
endlocal
exit /b 0

:no_python
echo [ERROR] No usable Python environment found.
echo.
echo   Looked for:
echo     %CONDA_PY%
echo     .venv\Scripts\python.exe  with a .livetranslate-ready marker
echo.
echo   Create the conda environment with:
echo     powershell -ExecutionPolicy Bypass -File setup-conda.ps1
echo.
pause
endlocal
exit /b 1

:venv_incomplete
echo [ERROR] The .venv exists but its setup is incomplete.
echo   Missing marker: .venv\.livetranslate-ready
echo   Re-run install.ps1, or use the conda environment instead:
echo     powershell -ExecutionPolicy Bypass -File setup-conda.ps1
echo.
pause
endlocal
exit /b 1
