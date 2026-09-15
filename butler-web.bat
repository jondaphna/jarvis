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

echo.
echo   Web page starting on http://localhost:3000
echo   Ctrl-C to stop.
echo.
npm run dev
pause
