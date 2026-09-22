@echo off
rem Kuaishou one-click GUI downloader (module 2)
setlocal
chcp 65001 >nul
set "HERE=%~dp0"
start "" "%HERE%runtime\kuaishou-env\Scripts\pythonw.exe" "%HERE%modules\kuaishou\tools\快手一键下载器.pyw"
