@echo off
REM Every check there is: the whole test suite, then the doctor.
REM Slower than butler-doctor.bat and more thorough - this one also
REM checks the joints, which is where things actually break.
setlocal
cd /d "%~dp0\jarvis_new"
echo.
echo   Checking everything. This takes about a minute.
echo.
uv run python -m pytest tests/ -q
echo.
echo   --------------------------------------------------
uv run python src/doctor.py
echo.
pause
