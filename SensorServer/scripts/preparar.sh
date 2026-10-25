#!/usr/bin/env bash
# Gera o certificado TLS (se faltar), sobe o banco (Docker) e aplica as migracoes. Idempotente. Usa o .env (SensorServer/.env). Rode de qualquer pasta.
set -euo pipefail
cd "$(dirname "$0")/.."
python scripts/gerar_certificado.py      # certificado TLS (so cria se faltar); o banco sobe com ele
docker compose up -d --wait
python -m flow_db migrar
python -m flow_db status
