@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "appdir=%~dp0."
if not exist "%appdir%\runtime\pythonw.exe" goto incomplete
if not exist "%appdir%\runtime\python314.zip" goto incomplete
if not exist "%appdir%\runtime\DLLs\_tkinter.pyd" goto incomplete
if not exist "%appdir%\launch.pyw" goto incomplete
if not exist "%appdir%\leakfinder\ui.py" goto incomplete
start "" "%appdir%\runtime\pythonw.exe" -B "%appdir%\launch.pyw" %*
if not errorlevel 1 exit /b 0
echo Windows could not start PLO Hero Leak Finder.
goto failed

:incomplete
echo Application files are missing. Choose "Extract all" in File Explorer,
echo open the extracted folder and double-click START.BAT.
echo Download the complete Windows x64 package from:
echo https://github.com/Texonja/plo-hero-leak-finder/releases/latest
:failed
pause
exit /b 1
