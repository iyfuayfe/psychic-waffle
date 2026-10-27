@echo off
rem ---------------------------------------------------------------
rem  CampusNet - setup wizard (first-time configuration)
rem  ASCII-only on purpose, see 0-*.bat for why.
rem
rem  Prefers CampusNetSetup.exe; falls back to source mode.
rem ---------------------------------------------------------------
title CampusNet Setup
cd /d "%~dp0"

if exist "CampusNetSetup.exe" goto runexe
if exist "dist\CampusNet\CampusNetSetup.exe" goto rundist
goto runsource

:runexe
start "" "CampusNetSetup.exe"
exit /b 0

:rundist
start "" "dist\CampusNet\CampusNetSetup.exe"
exit /b 0

:runsource
set "PYW="
where pythonw.exe >nul 2>nul
if not errorlevel 1 set "PYW=pythonw.exe"
if defined PYW goto runsrcpy
where pyw.exe >nul 2>nul
if not errorlevel 1 set "PYW=pyw.exe"
if defined PYW goto runsrcpy
goto noexe

:runsrcpy
start "" "%PYW%" "entry.py" --setup
exit /b 0

:noexe
echo.
echo   [ERROR] CampusNetSetup.exe was not found, and Python is not installed.
echo   Please extract the whole release folder and try again.
echo   Developers: install Python 3.9+ and run  python entry.py --setup
echo.
pause
exit /b 1
