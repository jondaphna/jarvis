@echo off
REM The little circle that sits on top of everything.
REM
REM Click it to open Jarvis. Drag it anywhere. Right-click for more.
REM It stays put when you close the browser, and it never shows up in
REM the taskbar or in alt-tab.
REM
REM Closing this window does NOT stop the orb. Right-click the orb and
REM choose "Hide the orb" to stop it.
setlocal
cd /d "%~dp0"

REM --with PyQt6 fetches the window toolkit on first run without touching
REM the agent's own dependency list.
start "" /min cmd /c "uv run --with PyQt6 python desktop_orb.py"
exit /b 0
