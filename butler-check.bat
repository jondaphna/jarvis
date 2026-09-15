@echo off
REM Are the stored keys valid? Asks Google and LiveKit directly.
setlocal
cd /d "%~dp0\jarvis_new"
echo.
uv run python setup_keys.py --check
echo.
pause
