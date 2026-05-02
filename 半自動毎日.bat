@echo off
chcp 65001 > nul
cd /d "%~dp0"
python monitor.py half_daily
echo.
pause
