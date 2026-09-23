@echo off
REM ============================================================
REM  LiveRecorder Login Helper
REM  Module 4 of the "Video Download Toolbox".
REM
REM  Opens a real browser so you can sign in to Kuaishou /
REM  Douyin. The cookies are then saved into the module config.
REM
REM  Why: Kuaishou blocks the per-room query API for anonymous
REM  visitors, so signing in is required to monitor any
REM  specific streamer.
REM
REM  Usage:  login.bat            (Kuaishou, then Douyin)
REM          login.bat kuaishou   (Kuaishou only)
REM          login.bat douyin     (Douyin only)
REM
REM  NOTE: keep this file ASCII-only and CRLF-terminated.
REM ============================================================
setlocal
cd /d "%~dp0"

REM ---------- pick an interpreter ----------
if exist "..\..\runtime\live-recorder-env\Scripts\python.exe" (
    set "PY=..\..\runtime\live-recorder-env\Scripts\python.exe"
    goto :check_deps
)
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
    goto :check_deps
)

where python >nul 2>nul
if errorlevel 1 goto :no_python
set "PY=python"

:check_deps
"%PY%" -c "import playwright" >nul 2>nul
if not errorlevel 1 goto :launch

echo [ERROR] playwright is NOT installed for this interpreter:
echo         %PY%
echo.
echo Run setup.bat first, or install it manually:
echo.
echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
echo     .venv\Scripts\python.exe -m playwright install chromium
echo.
pause
exit /b 1

:no_python
echo [ERROR] Python was not found in PATH.
echo         Install Python 3.10+ and check "Add Python to PATH",
echo         then double-click setup.bat to configure the environment.
echo.
pause
exit /b 1

:launch
chcp 65001 >nul
set PYTHONUTF8=1
echo.
echo ============================================================
echo   A browser window will open and the LOGIN POPUP with a
echo   QR CODE will appear automatically. Scan it with the
echo   Kuaishou / Douyin mobile app, then confirm on your phone.
echo.
echo   Tip: Kuaishou app, open the left sidebar, tap "Scan".
echo ============================================================
echo.
"%PY%" -u login.py %*
echo.
pause
exit /b 0
