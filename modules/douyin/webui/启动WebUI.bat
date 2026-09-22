@echo off
chcp 65001 >nul
title Douzy WebUI
cd /d "%~dp0"
echo.
echo   正在启动 Douzy WebUI（桥接工程真实数据）...
echo   浏览器访问: http://127.0.0.1:8518
echo.
"..\\venv\\Scripts\\python.exe" bridge_server.py
pause
