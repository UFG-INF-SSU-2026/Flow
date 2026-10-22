#!/usr/bin/env bash
# Prepara o banco e sobe borda e analise (a ordem entre os dois e indiferente). Ctrl+C desliga os dois, em ordem.
set -euo pipefail
cd "$(dirname "$0")/.."
./scripts/preparar.sh
python -m borda & PID_BORDA=$!
python -m analise & PID_ANALISE=$!
parar() {
  trap - INT TERM
  kill -TERM "$PID_ANALISE" "$PID_BORDA" 2>/dev/null || true
  wait "$PID_ANALISE" "$PID_BORDA" 2>/dev/null || true
}
trap parar INT TERM
wait "$PID_BORDA" "$PID_ANALISE" || true
parar
