@echo off
rem ---------------------------------------------------------------
rem  Campus Net Auto Login - Setup
rem  NOTE: intentionally ASCII-only. cmd.exe parses .bat files using
rem  the console code page, so non-ASCII text here can corrupt the
rem  batch file on some systems. All Chinese UI text lives in the
rem  Python GUI, which is Unicode-safe.
rem ---------------------------------------------------------------
title Campus Net Auto Login - Setup
cd /d "%~dp0"

set "PYW="
where pythonw.exe >nul 2>nul
if not errorlevel 1 set "PYW=pythonw.exe"
if defined PYW goto run
where pyw.exe >nul 2>nul
if not errorlevel 1 set "PYW=pyw.exe"
if defined PYW goto run
goto nopython

:run
start "" "%PYW%" "tools\gui_setup.pyw"
exit /b 0

:nopython
echo.
echo   [ERROR] Python was not found on this computer.
echo.
echo   Please install Python 3.8 or newer first:
echo       https://www.python.org/downloads/
echo.
echo   During installation, remember to tick the checkbox
echo   "Add python.exe to PATH" near the bottom of the installer.
echo.
echo   Opening the download page for you...
echo.
start "" "https://www.python.org/downloads/"
pause
exit /b 1