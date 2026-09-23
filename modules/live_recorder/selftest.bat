@echo off
REM ============================================================
REM  LiveRecorder Environment Self-Check
REM  Module 4 of the "Video Download Toolbox".
REM  Verifies Python deps, ffmpeg, config I/O, storage and the
REM  platform parsing layer. Run this if the app will not start.
REM
REM  NOTE: keep this file ASCII-only and CRLF-terminated.
REM ============================================================
setlocal
cd /d "%~dp0"

REM ---------- pick an interpreter ----------
if exist "..\..\runtime\live-recorder-env\Scripts\python.exe" (
    set "PY=..\..\runtime\live-recorder-env\Scripts\python.exe"
    goto :precheck
)
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
    goto :precheck
)

where python >nul 2>nul
if errorlevel 1 goto :no_python
set "PY=python"

:precheck
"%PY%" -c "import PyQt6" >nul 2>nul
if not errorlevel 1 goto :run

echo [WARN] PyQt6 is missing for interpreter: %PY%
echo        Step 1 of the self-test will fail.
echo        Double-click setup.bat to install all dependencies.
echo.

:run
chcp 65001 >nul
set PYTHONUTF8=1
echo Interpreter: %PY%
echo.
"%PY%" main.py --selftest
echo.
pause
exit /b 0

:no_python
echo [ERROR] Python was not found in PATH.
echo         Install Python 3.10+ and check "Add Python to PATH",
echo         then double-click setup.bat to configure the environment.
echo.
pause
exit /b 1
