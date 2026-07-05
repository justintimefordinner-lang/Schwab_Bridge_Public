@echo off
REM Runs the Schwab -> Google Sheets export using the project's virtual
REM environment. Task Scheduler points at this file. It changes into its
REM own folder first, so it works no matter where it's launched from, and
REM appends all output to export_log.txt so you can check unattended runs.

cd /d "%~dp0"
echo ---- %date% %time% ---- >> export_log.txt
".venv\Scripts\python.exe" export_to_sheets.py >> export_log.txt 2>&1
