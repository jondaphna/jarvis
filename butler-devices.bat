@echo off
REM List microphones and speakers, with the number to use for each.
setlocal
cd /d "%~dp0\jarvis_new"
echo.
echo   Your audio devices. Note the number next to the microphone you
echo   actually talk into, and the speakers you actually hear.
echo.
echo   Then run, for example:   butler-talk.bat 1 4
echo   (microphone 1, speakers 4)
echo.
uv run src/agent.py console --list-devices
pause
