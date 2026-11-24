#!/usr/bin/env bash
# Gera a CA e os certificados TLS (o que faltar), sobe o banco (Docker), aplica as migracoes e garante o papel da borda. Idempotente. Usa o .env (SensorServer/.env). Rode de qualquer pasta.
set -euo pipefail
cd "$(dirname "$0")/.."
python scripts/gerar_certificado.py --nome bridge --dns host.wokwi.internal      # CA, CA de dispositivos e certificados TLS (so cria o que faltar); o do bridge ja lista host.wokwi.internal (o nome do PC visto de dentro do Wokwi)
docker compose up -d --wait
python -m flow_db migrar
python -m flow_db status
