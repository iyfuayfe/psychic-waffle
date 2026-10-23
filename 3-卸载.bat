@echo off
rem ---------------------------------------------------------------
rem  CampusNet - uninstall
rem
rem  Stops the background daemon, removes the autostart entry and any
rem  leftovers from the old V2 design (scheduled task + startup VBS).
rem  Your account config and logs are KEPT unless you pass --remove-data.
rem  Nothing else on the system is touched, and no antivirus setting is
rem  modified (V3.0 never adds exclusions).
rem ---------------------------------------------------------------
title CampusNet Uninstall
cd /d "%~dp0"
echo.
echo   Stopping the background daemon and removing autostart ...
echo   (Your saved account and logs are kept.)
echo.

if exist "CampusNetCLI.exe" goto runexe
if exist "dist\CampusNet\CampusNetCLI.exe" goto rundist
goto runsource

:runexe
CampusNetCLI.exe uninstall %*
goto done

:rundist
dist\CampusNet\CampusNetCLI.exe uninstall %*
goto done

:runsource
where python.exe >nul 2>nul
if errorlevel 1 goto nopython
python entry.py --role cli uninstall %*
goto done

:nopython
echo   [ERROR] CampusNetCLI.exe was not found, and Python is not installed.
echo.
pause
exit /b 1

:done
echo.
echo   To also delete your saved account and logs, run:
echo       CampusNetCLI.exe uninstall --remove-data
echo.
pause
