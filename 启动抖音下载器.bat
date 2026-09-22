@echo off
rem Douyin downloader interactive CLI (module 1)
setlocal
chcp 65001 >nul
set "HERE=%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
title Douyin Downloader
cd /d "%HERE%modules\douyin"
"%HERE%runtime\douyin-venv\Scripts\python.exe" launcher.py %*
if errorlevel 1 pause
