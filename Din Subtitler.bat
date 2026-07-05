@echo off
setlocal
cd /d "%~dp0"
set "PATH=%~dp0bin;%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Execute "Instalar componentes.bat" primeiro.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "app_v2.py"
