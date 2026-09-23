@echo off
rem ============================================================
rem  LiveRecorder - live stream auto recorder (module 4)
rem  Double-click to open the recorder window (no console).
rem  Falls back to console mode so errors stay visible when the
rem  bundled runtime is missing.
rem
rem  NOTE: keep this file ASCII-only and CRLF-terminated.
rem ============================================================
setlocal
chcp 65001 >nul
set "HERE=%~dp0"
set "APP=%HERE%modules\live_recorder\main.py"
set "PYW=%HERE%runtime\live-recorder-env\Scripts\pythonw.exe"

if exist "%PYW%" goto :gui

set "PY=%HERE%runtime\live-recorder-env\Scripts\python.exe"
if not exist "%PY%" goto :noruntime
echo [WARN] pythonw.exe not found, running in console mode.
"%PY%" "%APP%"
if errorlevel 1 pause
exit /b 0

:gui
start "" "%PYW%" "%APP%"
exit /b 0

:noruntime
echo [ERROR] LiveRecorder runtime not found:
echo         %HERE%runtime\live-recorder-env
echo.
echo         Rebuild it with:
echo             python -m venv "%HERE%runtime\live-recorder-env"
echo             "%HERE%runtime\live-recorder-env\Scripts\python.exe" -m pip install -r "%HERE%modules\live_recorder\requirements.txt"
echo.
pause
exit /b 1
