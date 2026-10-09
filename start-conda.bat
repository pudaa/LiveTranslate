@echo off
REM ============================================================================
REM  LiveTranslate - one-click launch from the conda environment.
REM
REM  Why a separate script from start.bat:
REM    start.bat targets the project .venv that install.ps1 builds. This project
REM    runs on an isolated conda env instead, so it needs its own launcher.
REM
REM  Options:
REM    start-conda.bat            normal launch (console stays attached)
REM    start-conda.bat nogui      run the runtime self-checks instead of the app
REM    start-conda.bat gpu        check that ASR really loads on the GPU
REM ============================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "ENVNAME=livetranslate"
set "CONDA_PY=D:\DevVmEnv\Anaconda_envs\envs\%ENVNAME%\python.exe"
set "LOGDIR=%~dp0logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

if not exist "%CONDA_PY%" (
    echo [ERROR] conda env python not found:
    echo         %CONDA_PY%
    echo.
    echo Create the environment with:
    echo     powershell -ExecutionPolicy Bypass -File setup-conda.ps1
    echo.
    pause
    exit /b 1
)

if /i "%~1"=="nogui" (
    echo Running runtime self-checks...
    echo.
    "%CONDA_PY%" "%~dp0doc\migrations\verify_runtime.py"
    echo.
    "%CONDA_PY%" "%~dp0doc\migrations\verify_ui.py"
    echo.
    "%CONDA_PY%" -m pytest "%~dp0tests" -q
    echo.
    pause
    exit /b 0
)

if /i "%~1"=="gpu" (
    echo Checking that the ASR backend loads on the GPU...
    echo.
    "%CONDA_PY%" "%~dp0doc\migrations\check_gpu_asr.py"
    echo.
    pause
    exit /b 0
)

echo ============================================================
echo  LiveTranslate
echo  env : %ENVNAME%
echo  python: %CONDA_PY%
echo.
echo  Tip: run as  start-conda.bat nogui   for self-checks
echo       run as  start-conda.bat gpu      to verify GPU ASR
echo ============================================================
echo.
echo Starting... (close this window or press Ctrl+C to stop)
echo Full log: %LOGDIR%\app_start.log
echo.

REM Tee stdout+stderr to a log so a crash that kills the window is still
REM diagnosable afterwards. PowerShell's Tee-Object is the least noisy way to
REM do that on Windows without extra tooling.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "& '%CONDA_PY%' '%~dp0main.py' 2>&1 | Tee-Object -FilePath '%LOGDIR%\app_start.log'"

set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    echo [ERROR] LiveTranslate exited with code %RC%
    echo         See %LOGDIR%\app_start.log
    echo.
    pause
    exit /b %RC%
)
echo LiveTranslate exited normally.

endlocal
