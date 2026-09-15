@echo off
REM Talk to Jarvis in the terminal - no web page, no second window.
REM The quickest way to check the voice works at all.
setlocal
cd /d "%~dp0\jarvis_new"

if not exist ".env.local" (
  echo.
  echo   No keys yet. Run butler-setup.bat first.
  pause
  exit /b 1
)

echo.
echo   Console mode - just talk. Ctrl-C to stop.
echo.
uv run src/agent.py console
pause
