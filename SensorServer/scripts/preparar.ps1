# Sobe o banco (Docker), cria/ajusta os papeis flow_borda e flow_analise e aplica as migracoes. Idempotente.
# Usa o .env ADMINISTRATIVO (SensorServer\.env). Rode de qualquer pasta.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
docker compose up -d --wait;            if ($LASTEXITCODE) { exit $LASTEXITCODE }
python -m flow_db provisionar;          if ($LASTEXITCODE) { exit $LASTEXITCODE }
python -m flow_db migrar;               if ($LASTEXITCODE) { exit $LASTEXITCODE }
python -m flow_db status
