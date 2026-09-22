@echo off
rem ============================================================
rem  Video Download Toolbox - unified launcher (ASCII-only)
rem  Double-click to open the unified tkinter menu (toolbox.py)
rem ============================================================
setlocal
chcp 65001 >nul
set "HERE=%~dp0"
set "PY=%HERE%runtime\douyin-venv\Scripts\python.exe"
if exist "%PY%" goto :run
set "PY=%HERE%runtime\kuaishou-env\Scripts\python.exe"
if exist "%PY%" goto :run
for /f "delims=" %%P in ('where python 2^>nul') do (
    set "PY=%%P"
    goto :run
)
echo [ERROR] No Python runtime found in runtime\ or PATH.
pause
exit /b 1
:run
cd /d "%HERE%"
"%PY%" toolbox.py
if errorlevel 1 pause
