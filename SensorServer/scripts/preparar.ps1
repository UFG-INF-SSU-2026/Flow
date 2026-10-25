# Gera a CA e os certificados TLS (o que faltar), sobe o banco (Docker) e aplica as migracoes. Idempotente. Usa o .env (SensorServer\.env). Rode de qualquer pasta.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
python scripts/gerar_certificado.py;    if ($LASTEXITCODE) { exit $LASTEXITCODE }   # CA e certificados TLS (so cria o que faltar)
docker compose up -d --wait;            if ($LASTEXITCODE) { exit $LASTEXITCODE }
python -m flow_db migrar;               if ($LASTEXITCODE) { exit $LASTEXITCODE }
python -m flow_db status
