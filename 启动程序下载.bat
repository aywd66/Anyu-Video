@echo off
rem clawfetch direct-link downloader (module 3)
rem Double-click: prompts for a link, saves to downloads\direct
rem With args : passes them straight to clawfetch.py (original behavior)
setlocal EnableDelayedExpansion
chcp 65001 >nul
set "HERE=%~dp0"
set "PY=%HERE%runtime\douyin-venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
cd /d "%HERE%modules\clawfetch"
if "%~1"=="" (
    set /p "URL=输入下载链接(直链/中转页均可): "
    if not defined URL (
        echo 未输入链接,退出。
        pause
        exit /b 1
    )
    "%PY%" clawfetch.py "!URL!" -o "%HERE%downloads\direct"
) else (
    "%PY%" clawfetch.py %*
)
if errorlevel 1 pause
