@echo off
REM Talk to Jarvis in the terminal - no web page, no second window.
REM The quickest way to check the voice works at all.
REM
REM   butler-talk.bat          use Windows' default microphone and speakers
REM   butler-talk.bat 1        microphone 1
REM   butler-talk.bat 1 4      microphone 1, speakers 4
REM
REM Run butler-devices.bat to see the numbers.
setlocal
cd /d "%~dp0\jarvis_new"

if not exist ".env.local" (
  echo.
  echo   No keys yet. Run butler-setup.bat first.
  pause
  exit /b 1
)

set "DEVICES="
if not "%~1"=="" set "DEVICES=--input-device %~1"
if not "%~2"=="" set "DEVICES=%DEVICES% --output-device %~2"

echo.
echo   Console mode - just talk. Ctrl-C to stop.
if not "%DEVICES%"=="" echo   Using %DEVICES%
echo.
uv run src/agent.py console %DEVICES%
pause
