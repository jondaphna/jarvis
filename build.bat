@echo off
REM ==========================================================================
REM  Build JARVIS.exe  -  produces dist\JARVIS.exe, no Python needed to run it
REM ==========================================================================
setlocal

echo.
echo  ========================================
echo   Building JARVIS
echo  ========================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo  [X] Python isn't installed, or isn't on your PATH.
    echo      Get it from python.org - tick "Add Python to PATH" during setup.
    pause
    exit /b 1
)

echo  [1/3] Installing dependencies...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo  [X] Dependency install failed. Scroll up for the reason.
    pause
    exit /b 1
)

echo.
echo  [2/3] Running the test suite...
python -m pytest tests -q
if errorlevel 1 (
    echo.
    echo  [!] Tests failed. Building anyway, but something is wrong.
    echo.
)

echo.
echo  [3/3] Packaging JARVIS.exe...
python -m PyInstaller --noconfirm jarvis.spec
if errorlevel 1 (
    echo  [X] Build failed. Scroll up for the reason.
    pause
    exit /b 1
)

echo.
echo  ========================================
echo   Done.  dist\JARVIS.exe
echo  ========================================
echo.
echo   Double-click it. First run asks for your Claude API key.
echo.
pause
