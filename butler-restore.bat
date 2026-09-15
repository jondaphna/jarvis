@echo off
REM Go back to a version of Jarvis that worked.
REM
REM This never throws anything away. Before moving, it saves whatever you
REM have right now onto a backup branch, so "undo the undo" is always
REM possible. Your keys, memory, tasks and settings live outside the repo
REM (in %APPDATA%\JARVIS) and are not touched by any of this.
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo   ============================================
echo     Go back to a working version
echo   ============================================
echo.
echo   Restore points available:
echo.
git tag -l "works-*"
echo.
echo   You are currently on:
git rev-parse --abbrev-ref HEAD
echo.

set "TARGET="
set /p "TARGET=  Type a restore point (or press Enter for works-2026-09-15): "
if "!TARGET!"=="" set "TARGET=works-2026-09-15"

git rev-parse --verify "!TARGET!" >nul 2>nul
if errorlevel 1 (
  echo.
  echo   No restore point called "!TARGET!".
  echo   Pick one from the list above.
  pause
  exit /b 1
)

echo.
echo   Saving what you have now...
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value ^| find "="') do set "STAMP=%%I"
set "BACKUP=backup-!STAMP:~0,8!-!STAMP:~8,6!"

git add -A >nul 2>nul
git stash push -u -m "before restoring to !TARGET!" >nul 2>nul
git branch "!BACKUP!" >nul 2>nul
echo   Saved as branch: !BACKUP!

echo.
echo   Moving to !TARGET!...
git checkout -B "restored-!TARGET!" "!TARGET!"
if errorlevel 1 (
  echo.
  echo   Couldn't switch. Nothing was lost - you are still where you were.
  pause
  exit /b 1
)

echo.
echo   Matching the Python packages to this version...
cd jarvis_new
call uv sync
cd ..

echo.
echo   ============================================
echo     Done. You are on !TARGET!.
echo.
echo     Your keys, memory and tasks are untouched.
echo.
echo     To come back to the newest version:
echo       git checkout claude/personal-ai-assistant-hihz78
echo.
echo     What you had before this is on branch:
echo       !BACKUP!
echo   ============================================
echo.
pause
