@echo off
REM Start the officialdoc comment feedback server (writes knowledge\feedback.jsonl).
REM Add this to Task Scheduler (trigger: at log on) so it is always running.
cd /d "%~dp0"
set PYTHONUTF8=1
python feedback_server.py
