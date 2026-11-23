@echo off
setlocal
if exist "%~dp0runtime\pythonw.exe" (
    start "" "%~dp0runtime\pythonw.exe" -B "%~dp0launch.pyw"
) else (
    py -3 "%~dp0launch.pyw"
    if errorlevel 1 pause
)
