# Prepara o banco e abre borda e analise, cada uma na sua janela (a ordem entre as duas e indiferente).
# Para desligar: Ctrl+C em cada janela (desligamento ordenado) ou feche a janela.
$ErrorActionPreference = "Stop"
$raiz = Join-Path $PSScriptRoot ".."
& (Join-Path $PSScriptRoot "preparar.ps1"); if ($LASTEXITCODE) { exit $LASTEXITCODE }
Start-Process powershell -WorkingDirectory $raiz -ArgumentList "-NoExit", "-File", (Join-Path $PSScriptRoot "borda.ps1")
Start-Process powershell -WorkingDirectory $raiz -ArgumentList "-NoExit", "-File", (Join-Path $PSScriptRoot "analise.ps1")
