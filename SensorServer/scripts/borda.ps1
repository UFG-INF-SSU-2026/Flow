# Sobe a BORDA (processo proprio). Le so o .env.borda. Ctrl+C = desligamento ordenado.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
python -m borda
exit $LASTEXITCODE
