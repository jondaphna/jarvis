@echo off
REM Go back to a version of Jarvis that worked.
REM
REM This never throws anything away. Before moving, it saves whatever you have
REM right now onto a backup branch, so "undo the undo" is always possible.
REM Your keys, memory, tasks and settings live outside the repo (in
REM %APPDATA%\JARVIS) and are not touched by any of this.
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo   ============================================
echo     Go back to a working version
echo   ============================================
echo.
echo   Restore points:
echo.
for /f "usebackq tokens=1,2,3 delims=	" %%A in ("RESTORE_POINTS.txt") do (
  set "LINE=%%A"
  if not "!LINE:~0,1!"=="#" if not "!LINE!"=="" echo     %%A  -  %%C
)
echo.
echo   You are currently on:
git rev-parse --abbrev-ref HEAD
echo.

set "WANT="
set /p "WANT=  Which one? (Enter for 'working'): "
if "!WANT!"=="" set "WANT=working"

set "TARGET="
for /f "usebackq tokens=1,2 delims=	" %%A in ("RESTORE_POINTS.txt") do (
  if /i "%%A"=="!WANT!" set "TARGET=%%B"
)

if "!TARGET!"=="" (
  echo.
  echo   There is no restore point called "!WANT!".
  pause
  exit /b 1
)

git rev-parse --verify "!TARGET!" >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Your copy doesn't have that version yet. Fetching...
  git fetch origin
  git rev-parse --verify "!TARGET!" >nul 2>nul
  if errorlevel 1 (
    echo   Still can't find it. Run: git pull
    pause
    exit /b 1
  )
)

echo.
echo   Saving what you have now...
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value ^| find "="') do set "STAMP=%%I"
set "BACKUP=backup-!STAMP:~0,8!-!STAMP:~8,6!"

git add -A >nul 2>nul
git stash push -u -m "before restoring to !WANT!" >nul 2>nul
git branch "!BACKUP!" >nul 2>nul
echo   Saved as branch: !BACKUP!

echo.
echo   Moving to !WANT! (!TARGET!)...
git checkout -B "restored-!WANT!" "!TARGET!"
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
echo     Done. You are on "!WANT!".
echo.
echo     Your keys, memory and tasks are untouched.
echo.
echo     Back to the newest version:
echo       git checkout claude/personal-ai-assistant-hihz78
echo.
echo     What you had before this:
echo       !BACKUP!
echo   ============================================
echo.
pause
