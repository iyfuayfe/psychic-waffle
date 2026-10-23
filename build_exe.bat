@echo off
rem ---------------------------------------------------------------
rem  CampusNet - build the Windows EXE package (PyInstaller, onedir)
rem  ASCII-only on purpose, see 0-*.bat for why.
rem
rem  Requirements: Python 3.9+ (with tkinter) and
rem      python -m pip install -r requirements-dev.txt
rem  Output: dist\CampusNet\  (CampusNet.exe / CampusNetSetup.exe /
rem          CampusNetService.exe / CampusNetCLI.exe + _internal)
rem ---------------------------------------------------------------
setlocal
title CampusNet Build
cd /d "%~dp0"

where python.exe >nul 2>nul
if errorlevel 1 goto nopython

echo.
echo   Building CampusNet EXE ...
echo   ------------------------------------------------------------
python "tools\build_exe.py" %*
if errorlevel 1 goto failed

echo.
echo   ------------------------------------------------------------
echo   Build OK. Release folder: dist\CampusNet
echo.
echo   Next: run the smoke test
echo       python tools\test_exe_smoke.py
echo.
pause
exit /b 0

:nopython
echo.
echo   [ERROR] Python was not found on this computer.
echo.
echo   Building from source requires Python 3.9 or newer:
echo       https://www.python.org/downloads/
echo   During installation tick "Add python.exe to PATH".
echo.
pause
exit /b 1

:failed
echo.
echo   [ERROR] Build failed. See build\build.log for details.
echo.
pause
exit /b 1
