@echo off
REM Rebuilds TBS's project index so he knows the current state of the user's work.
REM Registered with Windows Task Scheduler as "TBS Project Index" (daily, 7:00 AM).
REM Safe to double-click any time to refresh on demand.

cd /d "%~dp0core"

py -3.11 digest.py
if errorlevel 1 (
    echo.
    echo Index refresh FAILED. Is Python 3.11 installed and on PATH?
    if not "%1"=="/quiet" pause
    exit /b 1
)

if not "%1"=="/quiet" (
    echo.
    pause
)
exit /b 0
