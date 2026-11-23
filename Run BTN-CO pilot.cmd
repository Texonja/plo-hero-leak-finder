@echo off
setlocal
cd /d "%~dp0"
set "PILOT_OUT=%LOCALAPPDATA%\PLO Hero Leak Finder\BTN CO pilot"
"runtime\python.exe" -B -m pilots.btn_co.run_pilot --output-dir "%PILOT_OUT%"
if errorlevel 1 (
  pause
  exit /b 1
)
start "" "%PILOT_OUT%\BTN-vs-CO.html"
