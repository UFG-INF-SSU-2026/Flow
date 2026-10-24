#!/usr/bin/env bash
# Sobe a ANALISE (processo proprio, so escuta em 127.0.0.1). Le do .env so FLOW_ANALISE_*. Ctrl+C = desligamento ordenado.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python -m analise
