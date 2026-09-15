@echo off
REM Window 1 - the agent. Start this BEFORE the web page.
setlocal
cd /d "%~dp0\jarvis_new"

if not exist ".env.local" (
  echo.
  echo   No keys yet. Run butler-setup.bat first.
  pause
  exit /b 1
)

echo.
echo   Starting Jarvis...
echo   Wait for "registered worker" before opening the web page.
echo   Ctrl-C to stop.
echo.
uv run src/agent.py dev
pause
