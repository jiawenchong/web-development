@echo off
REM Start the officialdoc web page + feedback server (http://10.10.28.170:8765/).
REM Run it from the folder that holds feedback_server.py (BASE_DIR).
REM On 10.10.28.170, add it to Task Scheduler with trigger "At startup".
pushd "%~dp0"
set PYTHONUTF8=1
python feedback_server.py
popd
