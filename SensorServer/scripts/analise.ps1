# Sobe a ANALISE (processo proprio, sem porta de rede). Le so o .env.analise. Ctrl+C = desligamento ordenado.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
python -m analise
exit $LASTEXITCODE
