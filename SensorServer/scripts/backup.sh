#!/usr/bin/env bash
# Backup do banco (pg_dump, formato custom) em SensorServer/backups/flow_<data>.dump, com o papel ADMINISTRATIVO.
# Com --testar, restaura o backup num banco descartavel (flow_restaurado), compara as contagens e o apaga: prova que o backup
# serve. Para a comparacao ser exata, pare a borda antes (senao leituras novas chegam entre o backup e a conferencia).
# Roda com o container flow-db no ar (scripts/preparar.sh). Usa o .env (FLOW_DB_ADMIN_USER e FLOW_DB_NAME; padrao flow e flow).
set -euo pipefail
cd "$(dirname "$0")/.."
valor() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r' || true; }
ADMIN="${FLOW_DB_ADMIN_USER:-$(valor FLOW_DB_ADMIN_USER)}"; ADMIN="${ADMIN:-flow}"
BANCO="${FLOW_DB_NAME:-$(valor FLOW_DB_NAME)}"; BANCO="${BANCO:-flow}"
ARQ="backups/${BANCO}_$(date +%Y%m%d_%H%M%S).dump"
DB() { docker compose exec -T db "$@"; }

mkdir -p backups
DB pg_dump -U "$ADMIN" -Fc -f /tmp/backup.dump "$BANCO"
docker compose cp db:/tmp/backup.dump "$ARQ"
DB rm -f /tmp/backup.dump
test -s "$ARQ" || { echo "erro: backup vazio: $ARQ" >&2; exit 1; }
echo "backup: $ARQ ($(du -h "$ARQ" | cut -f1))"

[ "${1:-}" = "--testar" ] || exit 0
TESTE="${BANCO}_restaurado"
docker compose cp "$ARQ" db:/tmp/restaurar.dump
DB psql -U "$ADMIN" -d postgres -q -c "DROP DATABASE IF EXISTS $TESTE WITH (FORCE)" -c "CREATE DATABASE $TESTE"
DB psql -U "$ADMIN" -d "$TESTE" -q -c "CREATE EXTENSION IF NOT EXISTS timescaledb" -c "SELECT timescaledb_pre_restore()" >/dev/null
DB pg_restore -U "$ADMIN" -d "$TESTE" --no-owner /tmp/restaurar.dump || echo "(avisos do pg_restore acima: o que vale e a comparacao abaixo)"
DB psql -U "$ADMIN" -d "$TESTE" -q -c "SELECT timescaledb_post_restore()" >/dev/null
CONTAGENS="SELECT (SELECT count(*) FROM leituras) AS leituras, (SELECT count(*) FROM alertas) AS alertas,
 (SELECT count(*) FROM confirmacoes) AS confirmacoes, (SELECT count(*) FROM episodios) AS episodios,
 (SELECT count(*) FROM contas) AS contas, (SELECT max(versao) FROM schema_migrations) AS migracao"
ORIGINAL="$(DB psql -U "$ADMIN" -d "$BANCO" -At -F' ' -c "$CONTAGENS")"
RESTAURADO="$(DB psql -U "$ADMIN" -d "$TESTE" -At -F' ' -c "$CONTAGENS")"
DB psql -U "$ADMIN" -d postgres -q -c "DROP DATABASE $TESTE WITH (FORCE)"
DB rm -f /tmp/restaurar.dump
echo "leituras alertas confirmacoes episodios contas migracao"
echo "original:   $ORIGINAL"
echo "restaurado: $RESTAURADO"
[ "$ORIGINAL" = "$RESTAURADO" ] && echo "restauracao integra" || { echo "ATENCAO: contagens diferentes (a borda estava gravando?)" >&2; exit 1; }
