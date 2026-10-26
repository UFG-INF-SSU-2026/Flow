#!/usr/bin/env bash
# Gera a CA e os certificados TLS (o que faltar), sobe o banco (Docker), aplica as migracoes e garante o papel da borda. Idempotente. Usa o .env (SensorServer/.env). Rode de qualquer pasta.
set -euo pipefail
cd "$(dirname "$0")/.."
python scripts/gerar_certificado.py      # CA e certificados TLS (so cria o que faltar); o banco sobe com o dele
docker compose up -d --wait
python -m flow_db migrar
python -m flow_db status
