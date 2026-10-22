#!/usr/bin/env bash
# Sobe o banco (Docker), cria/ajusta os papeis flow_borda e flow_analise e aplica as migracoes. Idempotente.
# Usa o .env ADMINISTRATIVO (SensorServer/.env). Rode de qualquer pasta.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose up -d --wait
python -m flow_db provisionar
python -m flow_db migrar
python -m flow_db status
