@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo [WARNING] Auto-login mode. May violate REINS ToS. Use manual mode instead.
echo.
python monitor.py auto_weekly
echo.
pause
