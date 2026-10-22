#!/usr/bin/env bash
# Sobe a BORDA (processo proprio). Le so o .env.borda. Ctrl+C = desligamento ordenado.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python -m borda
