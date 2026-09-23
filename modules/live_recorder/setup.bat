@echo off
REM ============================================================
REM  LiveRecorder - one-click environment setup
REM  Creates the project virtual environment (.venv) and installs
REM  every Python dependency listed in requirements.txt.
REM
REM  LiveRecorder - live stream auto recorder (personal use only)
REM
REM  NOTE: keep this file ASCII-only and CRLF-terminated.
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo   LiveRecorder - environment setup
echo ============================================================
echo.

REM ---------- 1/4 locate a Python interpreter ----------
where python >nul 2>nul
if errorlevel 1 goto :no_python

set "BASE="
for /f "delims=" %%i in ('where python') do (
    if not defined BASE set "BASE=%%i"
)
echo [1/4] Using Python: !BASE!

"!BASE!" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if errorlevel 1 goto :old_python
"!BASE!" --version
echo.

REM ---------- 2/4 create the virtual environment ----------
if exist ".venv\Scripts\python.exe" goto :venv_ok
echo [2/4] Creating virtual environment .venv ...
"!BASE!" -m venv .venv
if errorlevel 1 goto :venv_fail
goto :venv_done

:venv_ok
echo [2/4] Virtual environment .venv already exists - skipping.

:venv_done
set "PY=.venv\Scripts\python.exe"
echo.

REM ---------- 3/4 install dependencies ----------
echo [3/4] Installing dependencies (first run takes 1-3 minutes) ...
echo.
"%PY%" -m pip install --upgrade pip
echo.
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 goto :pip_fail
echo.

REM ---------- 4/4 verify ----------
echo [4/4] Verifying installation ...
"%PY%" -c "import PyQt6.QtCore as c; import httpx, requests; print('       PyQt6    ', c.PYQT_VERSION_STR); print('       httpx    ', httpx.__version__); print('       requests ', requests.__version__)"
if errorlevel 1 goto :verify_fail

echo.
echo ============================================================
echo   Setup complete. Double-click start.bat to launch the app.
echo ============================================================
echo.
echo Notes:
echo   1) Recording also needs ffmpeg - see README.md, section
echo      "Install ffmpeg" for three ways to set it up.
echo   2) Optional, for Douyin dynamic signatures:
echo        .venv\Scripts\playwright install chromium
echo.
pause
exit /b 0

REM ---------- error branches ----------
:no_python
echo [ERROR] Python was not found in PATH.
echo.
echo         Install Python 3.10 or newer and make sure
echo         "Add Python to PATH" is checked during setup.
echo         Download: https://www.python.org/downloads/windows/
echo.
pause
exit /b 1

:old_python
echo [ERROR] Python 3.10 or newer is required. Current version:
"!BASE!" --version
echo         Please upgrade Python and try again.
pause
exit /b 1

:venv_fail
echo [ERROR] Failed to create the virtual environment.
echo         If the venv module is missing, install the full
echo         Python distribution (not the embeddable package).
pause
exit /b 1

:pip_fail
echo.
echo [ERROR] Dependency installation failed. Usually a network issue.
echo         Retry with a domestic mirror:
echo.
echo         .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
echo.
pause
exit /b 1

:verify_fail
echo [ERROR] Verification failed. Please report the full output above.
pause
exit /b 1
