@echo off
REM Start the officialdoc comment feedback server.
REM Feedback is written to the folder this .bat (and feedback_server.py) lives in.
REM Works from a UNC path too, e.g. \\10.10.51.67\data\officialdoc\start_feedback_server.bat
pushd "%~dp0"
set PYTHONUTF8=1
python feedback_server.py
popd
