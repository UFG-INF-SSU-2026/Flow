#!/usr/bin/env bash
# Sobe a BORDA (processo proprio). Le o .env. Ctrl+C = desligamento ordenado.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python -m borda
