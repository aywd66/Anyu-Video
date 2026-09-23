@echo off
rem ============================================================
rem  Video Download Toolbox - unified launcher (ASCII-only)
rem
rem  WHAT CHANGED
rem    Before: ran python.exe in the foreground, so the cmd window
rem            stayed on the desktop the whole time the toolbox was open.
rem    Now:    uses start + pythonw.exe, which returns immediately and
rem            leaves no console behind. The toolbox keeps running headless.
rem
rem  NOTE ON FLICKER
rem    A .bat is executed by cmd.exe, so a console is created before the
rem    first line runs -- this file will always flash briefly. For a
rem    completely flicker-free start, double-click the .vbs launcher
rem    next to this file instead. This .bat is kept as the
rem    troubleshooting entry: if toolbox.py fails to compile it falls
rem    back to console mode and shows the traceback, instead of failing
rem    silently the way a headless launch would.
rem ============================================================
setlocal
chcp 65001 >nul
set "HERE=%~dp0"
set "PYDIR="

rem Pick the Scripts directory of the first available runtime.
for %%V in (douyin-venv kuaishou-env live-recorder-env) do (
    if not defined PYDIR if exist "%HERE%runtime\%%V\Scripts\pythonw.exe" set "PYDIR=%HERE%runtime\%%V\Scripts"
)
if not defined PYDIR (
    for /f "delims=" %%P in ('where pythonw 2^>nul') do (
        if not defined PYDIR set "PYDIR=%%~dpP"
    )
)
if not defined PYDIR (
    echo [ERROR] No Python runtime found in runtime\ or PATH.
    pause
    exit /b 1
)

cd /d "%HERE%"

rem Compile check first: a headless launch cannot report a syntax error
rem anywhere, so catch it here and fall back to console mode.
"%PYDIR%\pythonw.exe" -c "import py_compile;py_compile.compile(r'%HERE%toolbox.py',doraise=True)" >nul 2>nul
if errorlevel 1 goto :console

rem Normal path: hidden window, do not wait, leave nothing behind.
start "" "%PYDIR%\pythonw.exe" "%HERE%toolbox.py"
exit /b 0

:console
echo [DEBUG] toolbox.py failed the compile check.
echo [DEBUG] Falling back to console mode so you can read the error.
echo.
"%PYDIR%\python.exe" "%HERE%toolbox.py"
echo.
pause
exit /b 1
