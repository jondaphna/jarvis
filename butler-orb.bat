@echo off
REM The little circle that sits on top of everything.
REM
REM Starting it also starts Jarvis: the agent, the web app, and a Chrome
REM window already connected and listening. So once the orb is up you can
REM just say "hey Jarvis" - no clicking required.
REM
REM This window stays open while the orb runs. You can minimise it.
REM Closing it, or right-clicking the orb and choosing "Hide the orb",
REM stops the orb.
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
  echo.
  echo   uv isn't installed, or this window can't see it yet.
  echo   Run butler-setup.bat first. If you just installed it,
  echo   close this window, open a new one, and try again.
  echo.
  pause
  exit /b 1
)

echo.
echo   Starting the orb...
echo   The first run downloads the window toolkit, which takes a minute.
echo.

REM --no-project is load-bearing. Without it, uv sees the pyproject.toml in
REM this folder and rebuilds the whole jarvis package before running anything
REM - slow at best, and when it fails the orb simply never appears.
uv run --no-project --with PyQt6 python desktop_orb.py

if errorlevel 1 (
  echo.
  echo   The orb stopped with an error. The reason is above this line.
  echo.
  pause
  exit /b 1
)
exit /b 0
