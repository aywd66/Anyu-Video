@echo off
REM ============================================================
REM  LiveRecorder Launcher
REM  Module 4 of the "Video Download Toolbox".
REM  Starts the graphical interface.
REM
REM  Interpreter priority:
REM    1. toolbox runtime   ..\..\runtime\live-recorder-env
REM    2. module-local venv .venv
REM    3. "python" found in PATH
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

REM ---------- dependency check ----------
:check_deps
"%PY%" -c "import PyQt6" >nul 2>nul
if not errorlevel 1 goto :launch

echo [ERROR] PyQt6 is NOT installed for this interpreter:
echo         %PY%
echo.
set "ANS="
set /p "ANS=Install the dependencies now? [Y/N] "
if /i "%ANS%"=="Y" goto :auto_setup
goto :manual_hint

:auto_setup
if not exist "setup.bat" goto :manual_hint
call "setup.bat"
echo.
echo Setup finished. Please run start.bat again to launch the app.
pause
exit /b 0

:manual_hint
echo.
echo Run these commands manually to fix it:
echo.
echo     python -m venv .venv
echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
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

REM ---------- launch ----------
:launch
chcp 65001 >nul
set PYTHONUTF8=1
echo Starting LiveRecorder ...
"%PY%" main.py
if errorlevel 1 (
    echo.
    echo [ERROR] LiveRecorder exited with an error.
    echo         Run selftest.bat to check your environment.
    pause
)
exit /b 0
