$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $project ".venv\Scripts\python.exe"
Set-Location -LiteralPath $project

if (-not (Test-Path -LiteralPath $python)) {
    throw "Ambiente de desenvolvimento não encontrado. Execute Instalar componentes.ps1 primeiro."
}

& $python -m pip install "pyinstaller>=6.17,<7"
& $python -m PyInstaller --noconfirm --clean (Join-Path $project "Din Subtitler.spec")

$portable = Join-Path $project "dist\Din Subtitler"
New-Item -ItemType Directory -Force -Path (Join-Path $portable "models") | Out-Null
Write-Host ""
Write-Host "Portable criado em:" -ForegroundColor Green
Write-Host $portable
