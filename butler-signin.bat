@echo off
REM Sign in to your accounts, once.
REM
REM Jarvis drives its own browser. That browser keeps your logins, but it
REM starts out signed into nothing - which is why "open my Google" used to
REM show a stranger's Google.
REM
REM This opens that browser so you can sign in to Google, Netflix, Spotify,
REM YouTube, whatever you want Jarvis to reach. Do it once. It remembers.
REM
REM Close the window when you're done.
setlocal
cd /d "%~dp0\jarvis_new"
echo.
echo   ============================================
echo     Sign in to your accounts
echo   ============================================
echo.
echo   A browser window is opening. Sign in to whatever you want
echo   Jarvis to be able to reach:
echo.
echo     google.com     netflix.com     open.spotify.com
echo     youtube.com    gmail.com       anything else
echo.
echo   Stay signed in when it offers. Close the window when done.
echo   You only have to do this once.
echo.
uv run python src/signin.py
echo.
pause
