$ErrorActionPreference = "Stop"
$Host.UI.RawUI.WindowTitle = "Din Subtitler - Instalacao"

Write-Host ""
Write-Host "  DIN SUBTITLER" -ForegroundColor Magenta
Write-Host "  Preparando os componentes locais..." -ForegroundColor Gray
Write-Host ""

$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $appDir

if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "O Windows Package Manager (winget) nao foi encontrado. Atualize o 'App Installer' pela Microsoft Store."
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "[1/2] Instalando FFmpeg..." -ForegroundColor Cyan
    winget install --id Gyan.FFmpeg.Essentials --exact --accept-package-agreements --accept-source-agreements
} else {
    Write-Host "[1/2] FFmpeg ja instalado." -ForegroundColor Green
}

# Keep a stable local copy so the app works even before Windows refreshes PATH.
$ffmpegExe = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" `
    -Filter "ffmpeg.exe" -File -Recurse -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty FullName
if ($ffmpegExe) {
    New-Item -ItemType Directory -Force -Path (Join-Path $appDir "bin") | Out-Null
    Copy-Item -LiteralPath $ffmpegExe -Destination (Join-Path $appDir "bin\ffmpeg.exe") -Force
}

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    Write-Host "[2/2] Criando ambiente do aplicativo..." -ForegroundColor Cyan
    py -3 -m venv .venv
}

Write-Host "[2/2] Instalando Whisper, suporte a GPU e interface..." -ForegroundColor Cyan
& ".venv\Scripts\python.exe" -m pip install --upgrade pip
& ".venv\Scripts\python.exe" -m pip install -r requirements.txt

Write-Host ""
Write-Host "Instalacao concluida!" -ForegroundColor Green
Write-Host "O Whisper large-v3 sera baixado automaticamente no primeiro video." -ForegroundColor Gray
Write-Host ""
Read-Host "Pressione Enter para fechar"
