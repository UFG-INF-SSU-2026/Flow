# Backup do banco (pg_dump, formato custom) em SensorServer\backups\flow_<data>.dump, com o papel ADMINISTRATIVO.
# Com -Testar, restaura o backup num banco descartavel (flow_restaurado), compara as contagens e o apaga. Pare a borda antes,
# para a comparacao ser exata. Roda com o container flow-db no ar (scripts\preparar.ps1). Usa o .env (FLOW_DB_ADMIN_USER, FLOW_DB_NAME).
param([switch]$Testar)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
function Valor($nome, $padrao) {
    if (Test-Path .env) {
        $l = Select-String -Path .env -Pattern "^$nome=" | Select-Object -Last 1
        if ($l) { return $l.Line.Substring($nome.Length + 1).Trim() }
    }
    return $padrao
}
$admin = Valor "FLOW_DB_ADMIN_USER" "flow"
$banco = Valor "FLOW_DB_NAME" "flow"
$arq = "backups\{0}_{1}.dump" -f $banco, (Get-Date -Format "yyyyMMdd_HHmmss")
function DB { docker compose exec -T db @args; if ($LASTEXITCODE) { throw "falhou: $args" } }

New-Item -ItemType Directory -Force backups | Out-Null
DB pg_dump -U $admin -Fc -f /tmp/backup.dump $banco
docker compose cp db:/tmp/backup.dump $arq
docker compose exec -T db rm -f /tmp/backup.dump
if (-not (Test-Path $arq) -or (Get-Item $arq).Length -eq 0) { throw "backup vazio: $arq" }
"backup: $arq ({0:N0} KB)" -f ((Get-Item $arq).Length / 1KB)
if (-not $Testar) { exit 0 }

$teste = "${banco}_restaurado"
docker compose cp $arq db:/tmp/restaurar.dump
DB psql -U $admin -d postgres -q -c "DROP DATABASE IF EXISTS $teste WITH (FORCE)" -c "CREATE DATABASE $teste"
DB psql -U $admin -d $teste -q -c "CREATE EXTENSION IF NOT EXISTS timescaledb" -c "SELECT timescaledb_pre_restore()" | Out-Null
docker compose exec -T db pg_restore -U $admin -d $teste --no-owner /tmp/restaurar.dump   # avisos nao param: vale a comparacao
DB psql -U $admin -d $teste -q -c "SELECT timescaledb_post_restore()" | Out-Null
$contagens = "SELECT (SELECT count(*) FROM leituras), (SELECT count(*) FROM alertas), (SELECT count(*) FROM confirmacoes), (SELECT count(*) FROM episodios), (SELECT count(*) FROM contas), (SELECT max(versao) FROM schema_migrations)"
$original = (DB psql -U $admin -d $banco -At -F ' ' -c $contagens) -join ""
$restaurado = (DB psql -U $admin -d $teste -At -F ' ' -c $contagens) -join ""
DB psql -U $admin -d postgres -q -c "DROP DATABASE $teste WITH (FORCE)"
docker compose exec -T db rm -f /tmp/restaurar.dump
"leituras alertas confirmacoes episodios contas migracao"
"original:   $original"
"restaurado: $restaurado"
if ($original -eq $restaurado) { "restauracao integra" } else { Write-Error "contagens diferentes (a borda estava gravando?)"; exit 1 }
