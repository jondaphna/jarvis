@echo off
REM Open Jarvis's settings directly.
REM
REM This is the way in that cannot fail. The gear button floats above the call
REM screen, and anything that covers the call screen covers the gear with it.
REM This opens the settings page by its own address instead, where nothing can
REM sit on top of it.
REM
REM Here you set: standing instructions (what it should always do), a never-do
REM list, permissions, scheduled tasks, which AI thinks, what it remembers, and
REM your spoken commands.
setlocal
cd /d "%~dp0"

REM The key that signs this browser in. Without it the console opens on a
REM sign-in screen, which is correct but is not what you asked for. The script
REM creates the key on first run, so this works the very first time too.
set "URL=http://localhost:3000/settings"
for /f "usebackq delims=" %%K in (`node "%~dp0jarvis_new\frontend\scripts\dashboard-key.mjs" --path /settings 2^>nul`) do set "URL=%%K"

REM If the web app isn't up there is nothing to open, so start it first.
powershell -NoProfile -Command "try{(New-Object Net.Sockets.TcpClient).Connect('127.0.0.1',3000);exit 0}catch{exit 1}" >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Jarvis isn't running yet - starting it.
  echo   This takes a few seconds the first time.
  start "Jarvis agent" "%~dp0butler-agent.bat"
  start "Jarvis web" "%~dp0butler-web.bat"
  powershell -NoProfile -Command "1..40 ^| %%{ try{(New-Object Net.Sockets.TcpClient).Connect('127.0.0.1',3000);exit 0}catch{Start-Sleep -Milliseconds 1500} }; exit 1" >nul 2>nul
)

start "" "%URL%"
echo.
echo   Settings opened: http://localhost:3000/settings
echo.
exit /b 0
