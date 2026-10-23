@echo off
rem ---------------------------------------------------------------
rem  CampusNet - security check and diagnostics
rem
rem  V3.0 REPLACED the old "add to antivirus whitelist" script with this
rem  read-only diagnostic. Background: automatically excluding python.exe,
rem  the whole install folder and the whole project folder from Windows
rem  Defender widens the blind spot for *every* other program on the
rem  machine. This program does not need any exclusion to work, so the
rem  automatic exclusion code was removed entirely.
rem
rem  What this does instead:
rem    * shows the current network status with a clear reason
rem    * exports a REDACTED diagnostics bundle you can inspect or share
rem    * opens your user data folder
rem  Everything here is read-only and needs no administrator rights.
rem ---------------------------------------------------------------
title CampusNet Security and Diagnostics
cd /d "%~dp0"
echo.
echo   CampusNet - security check and diagnostics (read-only)
echo   ------------------------------------------------------------
echo.

if exist "CampusNetCLI.exe" goto runexe
if exist "dist\CampusNet\CampusNetCLI.exe" goto rundist
goto runsource

:runexe
set "CLI=CampusNetCLI.exe"
goto run

:rundist
set "CLI=dist\CampusNet\CampusNetCLI.exe"
goto run

:runsource
where python.exe >nul 2>nul
if errorlevel 1 goto nopython
set "CLI=python entry.py --role cli"
goto run

:run
echo   [1/3] Autostart and background daemon
%CLI% autostart status
echo.
echo   [2/3] Network status
%CLI% status
echo.
echo   [3/3] Exporting a redacted diagnostics bundle
%CLI% doctor
echo.
echo   ------------------------------------------------------------
echo   Passing "redirect" or "hijack" above means the campus portal
echo   wants authentication - that is normal before logging in.
echo   If you suspect antivirus interference, look at the
echo   protection history of your security software and at the
echo   daemon status above. Do NOT disable your antivirus.
echo.
start "" explorer "%LOCALAPPDATA%\CampusNet"
pause
exit /b 0

:nopython
echo   [ERROR] CampusNetCLI.exe was not found, and Python is not installed.
echo.
pause
exit /b 1
