@echo off
rem ASCII-only on purpose: batch files with non-ASCII chars get misparsed
rem when the active codepage changes mid-file. All Chinese output lives in
rem launcher.py / run.py, which render via the Windows console Unicode API.
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
title Douyin Downloader
cd /d "%~dp0"

"venv\Scripts\python.exe" launcher.py %*

if errorlevel 1 pause
