@echo off
REM Local launcher for the feedback server. Keep THIS file on your PC and put a
REM shortcut to it in the Startup folder (Win+R, shell:startup).
REM It waits until the 10.10.51.67 share is reachable, then starts
REM feedback_server.py from the share. Credentials come from Windows Credential
REM Manager (cmdkey /add:10.10.51.67 ...), so no password is stored here.
set "SHARE_DIR=\\10.10.51.67\d$\FAC_Web\agent_platform\html_files\data\officialdoc"
set /a TRIES=0
:wait
if exist "%SHARE_DIR%\feedback_server.py" goto start
set /a TRIES+=1
if %TRIES% geq 30 (
    echo Cannot reach %SHARE_DIR% after 5 minutes.
    echo Check the saved credential with: cmdkey /list
    pause
    exit /b 1
)
echo Waiting for %SHARE_DIR% ... (%TRIES%/30)
timeout /t 10 /nobreak >nul
goto wait
:start
pushd "%SHARE_DIR%"
set PYTHONUTF8=1
python feedback_server.py
popd
