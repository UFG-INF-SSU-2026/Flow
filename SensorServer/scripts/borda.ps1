# Sobe a BORDA (processo proprio). Le o .env. Ctrl+C = desligamento ordenado.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
python -m borda
exit $LASTEXITCODE
