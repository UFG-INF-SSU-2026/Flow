# Sobe o banco (Docker) e aplica as migracoes. Idempotente. Usa o .env (SensorServer\.env). Rode de qualquer pasta.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
docker compose up -d --wait;            if ($LASTEXITCODE) { exit $LASTEXITCODE }
python -m flow_db migrar;               if ($LASTEXITCODE) { exit $LASTEXITCODE }
python -m flow_db status
