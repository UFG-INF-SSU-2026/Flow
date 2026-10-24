#!/usr/bin/env bash
# Sobe o banco (Docker) e aplica as migracoes. Idempotente. Usa o .env (SensorServer/.env). Rode de qualquer pasta.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose up -d --wait
python -m flow_db migrar
python -m flow_db status
