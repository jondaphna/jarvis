@echo off
REM One-time setup for the Jarvis voice butler.
setlocal
cd /d "%~dp0"

echo.
echo   ============================================
echo     Jarvis - setup (run this once)
echo   ============================================
echo.

where uv >nul 2>nul
if errorlevel 1 (
  echo   Installing uv, the Python package manager this project uses...
  powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
  set "PATH=%USERPROFILE%\.local\bin;%PATH%"
  where uv >nul 2>nul
  if errorlevel 1 (
    echo.
    echo   uv installed but this window can't see it yet.
    echo   Close this window, open a new one, and run butler-setup.bat again.
    pause
    exit /b 1
  )
)

where node >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Node.js is missing - the web page needs it.
  echo   Install the LTS version from https://nodejs.org
  echo   then run this again.
  pause
  exit /b 1
)

cd jarvis_new

echo.
echo   [1/4] Python packages...
call uv sync || goto :failed

echo.
echo   [2/4] Browser for Jarvis to drive...
call uv run playwright install chromium || goto :failed

echo.
echo   [3/4] Your keys...
call uv run python setup_keys.py || goto :failed

echo.
echo   [4/4] Web page...
cd frontend
call npm install || goto :failed
cd ..

echo.
echo   ============================================
echo     Done.
echo.
echo     Start it with TWO windows, in this order:
echo       1. butler-agent.bat    ^(wait for "registered worker"^)
echo       2. butler-web.bat
echo.
echo     Then open  http://localhost:3000
echo   ============================================
echo.
pause
exit /b 0

:failed
echo.
echo   Setup stopped on the step above. The error is printed there.
pause
exit /b 1
