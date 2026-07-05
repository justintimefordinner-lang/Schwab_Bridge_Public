@echo off
REM Starts the Schwab -> app/Sheets auto-pusher. Double-click to run, or point
REM Windows Task Scheduler at this file to launch it automatically at log on.
cd /d "%~dp0"
call .venv\Scripts\activate
python auto_push.py
pause
