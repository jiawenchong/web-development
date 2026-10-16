@echo off
REM Stop the background officialdoc web page + feedback server (whatever listens on port 8765).
REM If it was started by Task Scheduler as SYSTEM, run this as administrator.
set "FOUND="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /C:"0.0.0.0:8765 "') do (
    set "FOUND=1"
    taskkill /PID %%p /F
)
if not defined FOUND echo Not running.
timeout /t 3 >nul
