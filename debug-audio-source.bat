@echo off
REM ============================================================================
REM  LiveTranslate - audio-source diagnostics (A / B / C)
REM
REM  Runs every probe used to investigate per-application audio capture, and
REM  writes the output to logs\audio-source-debug.log so it can be sent back
REM  verbatim.
REM
REM  Written with one command per line and no nested ( ) blocks: a caret-escaped
REM  parenthesis inside a redirected block closes the block early and cmd then
REM  tries to execute the next line as a command.
REM
REM  Usage:
REM    debug-audio-source.bat          diagnostics only (silent)
REM    debug-audio-source.bat tone     also run the capture PoC, which plays a
REM                                    short, quiet tone
REM ============================================================================
setlocal
cd /d "%~dp0"

set "PY=D:\DevVmEnv\Anaconda_envs\envs\livetranslate\python.exe"
set "LOGDIR=%~dp0logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOG=%LOGDIR%\audio-source-debug.log"

if not exist "%PY%" goto no_python

echo Logging to %LOG%
echo This takes about a minute.
echo.

echo ============================================================ > "%LOG%"
echo  LiveTranslate audio-source diagnostics >> "%LOG%"
echo  date: %DATE% %TIME% >> "%LOG%"
echo  python: %PY% >> "%LOG%"
echo ============================================================ >> "%LOG%"

echo. >> "%LOG%"
echo ---- 1 of 4: COM apartment model ---- >> "%LOG%"
"%PY%" -u "doc\migrations\probe_com_apartment.py" >> "%LOG%" 2>&1

echo. >> "%LOG%"
echo ---- 2 of 4: which argument is rejected - A/B/C/D bisect ---- >> "%LOG%"
"%PY%" -u "doc\migrations\probe_activation_args.py" >> "%LOG%" 2>&1

echo. >> "%LOG%"
echo ---- 3 of 4: FTM-agile completion handler - the decisive test ---- >> "%LOG%"
"%PY%" -u "doc\migrations\probe_agile_handler.py" >> "%LOG%" 2>&1

echo. >> "%LOG%"
echo ---- 4 of 4: session enumeration - who is playing, on which device ---- >> "%LOG%"
"%PY%" -u "doc\migrations\probe_audio_source.py" >> "%LOG%" 2>&1

if /i not "%~1"=="tone" goto show

echo. >> "%LOG%"
echo ---- extra: process loopback end-to-end (plays a quiet tone) ---- >> "%LOG%"
"%PY%" -u "doc\migrations\check_process_loopback.py" >> "%LOG%" 2>&1

:show
echo. >> "%LOG%"
echo ============================================================ >> "%LOG%"
echo  done >> "%LOG%"
echo ============================================================ >> "%LOG%"

type "%LOG%"
echo.
echo ------------------------------------------------------------
echo  Full log: %LOG%
echo  If the result is still E_ILLEGAL_METHOD_CALL, send that file
echo  back and it will show exactly which step failed.
echo ------------------------------------------------------------
echo.
if /i "%~1"=="tone" echo NOTE: the extra PoC played a short quiet tone.
if /i "%~1"=="tone" echo.
pause
exit /b 0

:no_python
echo [ERROR] conda env python not found:
echo         %PY%
echo.
echo Create the environment first:
echo     powershell -ExecutionPolicy Bypass -File setup-conda.ps1
echo.
pause
exit /b 1
