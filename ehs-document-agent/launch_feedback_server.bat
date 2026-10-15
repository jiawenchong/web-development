@echo off
REM Local launcher for the feedback server. Keep THIS file on your PC and put a
REM shortcut to it in the Startup folder (Win+R, shell:startup).
REM It waits until the 10.10.51.67 share is reachable, then starts
REM feedback_server.py from the share. If the share needs a password, it opens
REM the share in Explorer once so Windows shows the sign-in prompt; after you
REM sign in, the server starts by itself. No password is stored anywhere.
set "SHARE_ROOT=\\10.10.51.67\d$"
set "SHARE_DIR=%SHARE_ROOT%\FAC_Web\agent_platform\html_files\data\officialdoc"
set "PROMPTED="
:wait
if exist "%SHARE_DIR%\feedback_server.py" goto start
if not defined PROMPTED (
    set "PROMPTED=1"
    echo Cannot reach %SHARE_DIR%
    echo Opening it in Explorer - sign in there, then this window continues by itself.
    start "" explorer "%SHARE_ROOT%"
)
timeout /t 15 /nobreak >nul
goto wait
:start
echo Share reachable, starting feedback server...
pushd "%SHARE_DIR%"
set PYTHONUTF8=1
python feedback_server.py
popd
