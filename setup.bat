@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0setup.ps1"
if errorlevel 1 goto failed
echo Ready. Double-click start.bat to open Fatfish.
pause
exit /b 0
:failed
echo Setup failed. Check the message above and retry setup.bat.
pause
exit /b 1
