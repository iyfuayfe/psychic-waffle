@echo off
rem ---------------------------------------------------------------
rem  CampusNet - main window (GUI)
rem
rem  NOTE: intentionally ASCII-only inside the .bat itself. cmd.exe
rem  parses .bat files with the console code page, so non-ASCII text
rem  can corrupt the file on some systems. All Chinese UI text lives
rem  in the Tk interface.
rem
rem  V3.0: prefers the packaged EXE. Falls back to source mode
rem  (pythonw + entry.py) only when the EXE is not present.
rem ---------------------------------------------------------------
title CampusNet
cd /d "%~dp0"

if exist "CampusNet.exe" goto runexe
if exist "dist\CampusNet\CampusNet.exe" goto rundist
goto runsource

:runexe
start "" "CampusNet.exe"
exit /b 0

:rundist
start "" "dist\CampusNet\CampusNet.exe"
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
start "" "%PYW%" "entry.py"
exit /b 0

:noexe
echo.
echo   [ERROR] CampusNet.exe was not found, and Python is not installed.
echo.
echo   If you downloaded the release package, make sure you extracted the
echo   WHOLE folder (CampusNet.exe needs the _internal folder next to it).
echo.
echo   Developers: install Python 3.9+ and run  python entry.py
echo.
pause
exit /b 1
