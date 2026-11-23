@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "appdir=%~dp0PLO Hero Leak Finder"
if exist "%appdir%\" goto portable
set "appdir=%~dp0."
if exist "%appdir%\runtime\" goto portable
if not exist "%appdir%\launch.pyw" goto incomplete
if not exist "%appdir%\leakfinder\ui.py" goto incomplete

rem Source checkout: use an installed Python.
where.exe py.exe >nul 2>&1
if errorlevel 1 goto source_python
py.exe -3 "%appdir%\launch.pyw" %*
goto source_done

:source_python
python.exe "%appdir%\launch.pyw" %*
:source_done
if not errorlevel 1 exit /b 0
echo Starting from source requires Python 3 with Tk.
goto failed

:portable
if not exist "%appdir%\runtime\pythonw.exe" goto incomplete
if not exist "%appdir%\launch.pyw" goto incomplete
if not exist "%appdir%\leakfinder\ui.py" goto incomplete
start "" "%appdir%\runtime\pythonw.exe" -B "%appdir%\launch.pyw" %*
if not errorlevel 1 exit /b 0
echo Windows could not start PLO Hero Leak Finder.
goto failed

:incomplete
echo The application files are missing. Open the ZIP in File Explorer,
echo choose "Extract all", then double-click START.BAT in the extracted folder.
:failed
pause
exit /b 1
