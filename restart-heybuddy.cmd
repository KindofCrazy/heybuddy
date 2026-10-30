@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-qq.ps1" -Restart
if errorlevel 1 pause
