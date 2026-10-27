@echo off
rem ---------------------------------------------------------------
rem  CampusNet - network status / diagnosis (console output)
rem  ASCII-only on purpose, see 0-*.bat for why.
rem ---------------------------------------------------------------
title CampusNet Status
cd /d "%~dp0"

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
echo.
echo   Checking network status ...
echo   ------------------------------------------------------------
%CLI% status
echo   ------------------------------------------------------------
echo.
echo   Info: "redirect" / "hijack" means the campus portal is asking
echo   for authentication. "dns-error" / "timeout" / "unreachable"
echo   means the network layer is the problem, not your password.
echo.
pause
exit /b 0

:nopython
echo.
echo   [ERROR] CampusNetCLI.exe was not found, and Python is not installed.
echo   Please extract the whole release folder and try again.
echo.
pause
exit /b 1
