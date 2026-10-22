#!/usr/bin/env bash
# Sobe a ANALISE (processo proprio, sem porta de rede). Le so o .env.analise. Ctrl+C = desligamento ordenado.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python -m analise
