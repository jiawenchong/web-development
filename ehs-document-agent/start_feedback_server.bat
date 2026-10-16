@echo off
REM Start the officialdoc web page + feedback server in the BACKGROUND (no window).
REM Double-click once. It keeps running until this PC restarts or you SIGN OUT
REM (closing / disconnecting the remote desktop window is fine).
REM To stop it: stop_feedback_server.bat     Log: feedback_server.log
pushd "%~dp0"
set PYTHONUTF8=1
set "CHECK=import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8765/health', timeout=3)"

python -c "%CHECK%" >nul 2>&1
if not errorlevel 1 (
    echo Already running: http://127.0.0.1:8765/
    goto done
)

start "" pythonw feedback_server.py
timeout /t 3 /nobreak >nul

python -c "%CHECK%" >nul 2>&1
if errorlevel 1 (
    echo FAILED to start. See feedback_server.log in %CD%
    pause
    goto done
)
echo Started in background: http://127.0.0.1:8765/

:done
timeout /t 3 >nul
popd
