@echo off
REM Run the Chat Data Extractor locally. Double-click this, or run it from a terminal,
REM and leave the window open — the app stays up until you close it or press Ctrl+C.
cd /d "%~dp0"
echo Starting Chat Data Extractor...  (dashboard: http://127.0.0.1:8000)
python -m app
echo.
echo App stopped. Press any key to close.
pause >nul
