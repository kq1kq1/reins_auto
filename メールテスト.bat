@echo off
chcp 65001 > nul
cd /d "%~dp0"
python monitor.py test_mail
echo.
pause
