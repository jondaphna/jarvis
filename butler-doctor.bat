@echo off
REM Check everything and say what's wrong.
REM Run this when something isn't working and send me what it prints.
setlocal
cd /d "%~dp0\jarvis_new"
uv run python src/doctor.py
echo.
pause
