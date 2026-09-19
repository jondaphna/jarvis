@echo off
REM Re-enter your keys, and test them before writing anything.
setlocal
cd /d "%~dp0\jarvis_new"
echo.
echo   Re-entering your keys. Paste each one on its own - just the key,
echo   no command, nothing else on the line.
echo.
uv run python setup_keys.py --reset
pause
