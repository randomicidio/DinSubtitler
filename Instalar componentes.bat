@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Instalar componentes.ps1"
if errorlevel 1 (
  echo.
  echo A instalacao encontrou um problema.
  pause
)
