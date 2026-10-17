@echo off
rem ---------------------------------------------------------------
rem  Campus Net Auto Login - Add to antivirus whitelist
rem  NOTE: intentionally ASCII-only. cmd.exe parses .bat files using
rem  the console code page, so non-ASCII text here can corrupt the
rem  batch file on some systems. All Chinese UI text lives in the
rem  PowerShell script, which is Unicode-safe.
rem ---------------------------------------------------------------
title Campus Net Auto Login - Whitelist
cd /d "%~dp0"
echo.
echo   Adding this program to the antivirus whitelist ...
echo   Windows Defender is handled automatically (needs admin).
echo   If you have 360 / Huorong / Tencent PC Manager installed,
echo   the script will print manual steps for them.
echo.
echo   A "User Account Control" window will pop up - please click "Yes".
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "tools\whitelist.ps1" -Apply
echo.
pause
