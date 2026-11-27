@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "appdir=%~dp0."
if not exist "%appdir%\launch.pyw" goto incomplete
if not exist "%appdir%\leakfinder\ui.py" goto incomplete
if exist "%appdir%\runtime\" goto portable
if exist "%appdir%\PLO Hero Leak Finder.exe" goto incomplete
goto source

:portable
if not exist "%appdir%\runtime\pythonw.exe" goto incomplete
if not exist "%appdir%\runtime\python314.zip" goto incomplete
if not exist "%appdir%\runtime\DLLs\_tkinter.pyd" goto incomplete
start "" "%appdir%\runtime\pythonw.exe" -B "%appdir%\launch.pyw" %*
if not errorlevel 1 exit /b 0
echo Windows could not start PLO Hero Leak Finder.
goto failed

:source
for %%P in (py.exe) do set "py_launcher=%%~$PATH:P"
if not defined py_launcher goto try_python
"%py_launcher%" -3 -c "import sys, tkinter; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if errorlevel 1 goto try_python
"%py_launcher%" -3 "%appdir%\launch.pyw" %*
exit /b %errorlevel%

:try_python
for %%P in (python.exe) do set "source_python=%%~$PATH:P"
if not defined source_python goto source_help
if /i "%source_python%"=="%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe" goto source_help
"%source_python%" -c "import sys, tkinter; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if errorlevel 1 goto source_help
"%source_python%" "%appdir%\launch.pyw" %*
exit /b %errorlevel%

:source_help
echo This is the source code. It requires Python 3.11 or later with Tk.
echo To play back hands without installing anything, download the Windows x64 ZIP:
echo https://github.com/Texonja/plo-hero-leak-finder/releases/latest
goto failed

:incomplete
echo Application files are missing. Choose "Extract all" in File Explorer,
echo open the extracted folder and double-click START.BAT.
echo Download the complete Windows x64 package from:
echo https://github.com/Texonja/plo-hero-leak-finder/releases/latest
:failed
pause
exit /b 1
