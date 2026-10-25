# Gera o certificado TLS (se faltar), sobe o banco (Docker) e aplica as migracoes. Idempotente. Usa o .env (SensorServer\.env). Rode de qualquer pasta.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
python scripts/gerar_certificado.py;    if ($LASTEXITCODE) { exit $LASTEXITCODE }   # certificado TLS (so cria se faltar)
docker compose up -d --wait;            if ($LASTEXITCODE) { exit $LASTEXITCODE }
python -m flow_db migrar;               if ($LASTEXITCODE) { exit $LASTEXITCODE }
python -m flow_db status
