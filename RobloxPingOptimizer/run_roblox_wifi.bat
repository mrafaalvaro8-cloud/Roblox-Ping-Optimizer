@echo off
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "SCRIPT=%~dp0roblox_wifi_ping_optimizer.py"

where python >nul 2>&1
if errorlevel 1 (
    echo Python was not found on PATH.
    echo Install Python 3 from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during setup.
    pause
    exit /b 1
)

if not exist "%SCRIPT%" (
    echo Cannot find %SCRIPT%
    pause
    exit /b 1
)

echo ==================================================
echo  Roblox setup.roblox.com Latency Optimizer
echo  Focus: keep setup.roblox.com under 50 ms
echo ==================================================
echo.
echo  What do you want to do?
echo.
echo   [1]  QUICK SCAN ONLY      -- find fastest edge, no monitoring
echo   [2]  SCAN + MONITOR 30min -- run 30 min, watch SLA
echo   [3]  SCAN + MONITOR 10min -- shorter session
echo   [4]  APPLY HOSTS (admin)  -- scan + lock best edge to hosts
echo   [5]  RESTORE HOSTS (admin)-- revert hosts file from backup
echo   [6]  EXIT
echo.
set "c="
set /p c="Choice [1-6]: "

if "%c%"=="1" goto scan
if "%c%"=="2" goto run30
if "%c%"=="3" goto run10
if "%c%"=="4" goto apply
if "%c%"=="5" goto restore
if "%c%"=="6" exit /b 0
echo Invalid.
pause
exit /b 1

:scan
echo.
echo Running scan-only...
echo.
python "%SCRIPT%" --scan-only
goto done

:run30
echo.
echo Running 30-minute session...
echo Close this and re-run as admin for --apply-hosts later.
echo.
python "%SCRIPT%" -m 30
goto done

:run10
echo.
echo Running 10-minute session...
echo.
python "%SCRIPT%" -m 10
goto done

:apply
net session >nul 2>&1
if %errorlevel% NEQ 0 (
    echo Requesting administrator...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)
echo.
echo Running scan + hosts lock (ADMIN)...
echo.
python "%SCRIPT%" --apply-hosts -m 15
goto done

:restore
net session >nul 2>&1
if %errorlevel% NEQ 0 (
    echo Requesting administrator...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)
echo.
python "%SCRIPT%" --restore
goto done

:done
echo.
echo Logs in: %LOCALAPPDATA%\roblox_setup_optimizer\
pause
endlocal
