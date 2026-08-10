@echo off
title Shuiyuan Agent Launcher
cd /d "%~dp0"

rem ============================================================
rem  Shuiyuan Agent one-click launcher
rem  - If the service is already running, just open the browser.
rem  - Otherwise start the service in a minimized window and
rem    open the browser automatically when it is ready.
rem  To stop the service: close the "Shuiyuan Agent Service"
rem  window in the taskbar.
rem ============================================================

echo [1/3] Checking if port 8000 is already in use ...

netstat -ano | findstr ":8000" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 goto already_running

echo [2/3] Service not running, starting it now ...
start "Shuiyuan Agent Service" /min cmd /c "cd /d %~dp0 && python web_app.py"

echo [3/3] Waiting for the service to become ready ...
set /a tries=0
:waitloop
set /a tries+=1
netstat -ano | findstr ":8000" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 goto ready
if %tries% geq 60 goto timeout
ping -n 2 127.0.0.1 >nul
goto waitloop

:already_running
echo [3/3] Service already running, opening browser ...

:ready
start "" "http://127.0.0.1:8000"
echo.
echo Browser opened. Enjoy!
ping -n 3 127.0.0.1 >nul
exit /b 0

:timeout
echo.
echo Startup timed out (60s). Check the "Shuiyuan Agent Service"
echo window in the taskbar for error details.
pause
exit /b 1
