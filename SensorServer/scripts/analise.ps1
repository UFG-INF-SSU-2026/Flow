# Sobe a ANALISE (processo proprio, so escuta em 127.0.0.1). Le do .env so FLOW_ANALISE_*. Ctrl+C = desligamento ordenado.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
python -m analise
exit $LASTEXITCODE
