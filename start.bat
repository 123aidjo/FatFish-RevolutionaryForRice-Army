@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto setup
".venv\Scripts\python.exe" -c "import tkinter, fastapi, uvicorn, httpx, PIL, tomli_w, websockets, bilibili_api, imageio_ffmpeg" >nul 2>nul
if errorlevel 1 goto setup
goto launch
:setup
  echo First launch: preparing the project Python environment.
  powershell.exe -NoLogo -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0setup.ps1"
  if errorlevel 1 goto failed
:launch
powershell.exe -NoLogo -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0start.ps1"
if errorlevel 1 goto failed
exit /b 0
:failed
echo Startup failed. See the message above. No existing QQ process was stopped.
pause
exit /b 1
