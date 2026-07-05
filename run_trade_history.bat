@echo off
REM Daily one-shot: refresh the rolling 7-day trade history, then exit.
REM Point Windows Task Scheduler at this file with a daily trigger AFTER the
REM market close (4:00pm ET = 2:00pm your time; ~2:15pm is a safe trigger).
cd /d "%~dp0"
call .venv\Scripts\activate
python sync_trade_history.py
