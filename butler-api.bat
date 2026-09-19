@echo off
REM Jarvis's control service - the thing the Settings panel talks to.
REM
REM Handles your settings, scheduled tasks, AI keys and memory. Binds to
REM this machine only and requires a token, because it can write API keys
REM into your vault.
REM
REM butler-agent.bat starts this for you, so you rarely need it by hand.
setlocal
cd /d "%~dp0\jarvis_new"
echo.
uv run python src/control_api.py
pause
