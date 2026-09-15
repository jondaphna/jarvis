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

REM The control service powers the Settings panel in the web page. It runs
REM in its own minimised window so this one stays readable.
start "Jarvis control" /min cmd /c "uv run python src/control_api.py"

echo.
echo   Starting Jarvis...
echo   Wait for "registered worker" before opening the web page.
echo   Ctrl-C to stop.
echo.
uv run src/agent.py dev
pause
