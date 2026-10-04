@echo off
:: Self-elevate to Administrator
net session >nul 2>&1
if %errorlevel% NEQ 0 (
    echo Requesting administrator privileges...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

where python >nul 2>&1
if errorlevel 1 (
    echo Python was not found on PATH.
    echo Install Python 3 from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during setup.
    pause
    exit /b 1
)

echo ==================================================
echo  Roblox Wi-Fi Ping Optimizer -- ADMIN MODE
echo ==================================================
echo  Scans every discovered edge for setup.roblox.com
echo  and, if one passes the stability gate (same edge
echo  across scan rounds, under 50 ms, low jitter),
echo  locks it in the hosts file.
echo.
echo  Nothing is written unless the gate passes.
echo  Revert anytime with run_roblox_wifi.bat option [5]
echo  or:  python roblox_wifi_ping_optimizer.py --restore
echo ==================================================
pause

python "%~dp0roblox_wifi_ping_optimizer.py" --apply-hosts -m 15

echo.
echo ==================================================
echo  Done. If the hosts file was changed, restart your
echo  browser / Roblox to pick it up. Run --restore to
echo  revert, or if Roblox misbehaves later.
echo ==================================================
pause
endlocal
