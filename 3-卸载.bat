@echo off
rem  Campus Net Auto Login - Uninstall  (ASCII-only on purpose, see 1-*.bat)
title Campus Net Auto Login - Uninstall
cd /d "%~dp0"
echo.
echo   Removing the auto-login scheduled task and stopping the background process...
echo   (Your saved account config and logs are kept.)
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "tools\uninstall_task.ps1"
echo.
pause