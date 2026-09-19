@echo off
REM Window 2 - the web page. Start the agent first.
setlocal
cd /d "%~dp0\jarvis_new\frontend"

if not exist ".env.local" (
  echo.
  echo   No keys yet. Run butler-setup.bat first.
  pause
  exit /b 1
)

REM Also started here, not only by butler-agent.bat. The settings panel is
REM part of the web app, so it has to work whenever the web app is running -
REM otherwise opening just the page gives you a settings screen that does
REM nothing, with no clue why. Starting twice is harmless; the second one
REM sees the port taken and steps aside.
start "Jarvis control" /min uv run --project "%~dp0jarvis_new" python "%~dp0jarvis_new\src\control_api.py"

echo.
echo   Web page starting on http://localhost:3000
echo   Ctrl-C to stop.
echo.
npm run dev
pause
