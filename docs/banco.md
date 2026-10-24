# Banco de dados (PostgreSQL + TimescaleDB) — parte 1

Estado: camada de acesso, esquema, permissões, e o servidor separado em **borda** e **análise**, ligadas só por este banco.
A parte 2 (retenção, backup, remoção da `telemetria` e dos arquivos de transição, contas/sessões no banco) ainda não foi feita; as seções abaixo dizem o que ela vai mudar.

## Subir do zero (a partir de `SensorServer/`)

```
cp .env.example .env     # preencha FLOW_JWT_SECRET, FLOW_DB_PASSWORD, FLOW_DB_BORDA_PASSWORD, FLOW_DB_ANALISE_PASSWORD
docker compose up -d     # espere ficar healthy
pip install -r requirements.txt
python -m flow_db provisionar   # cria/ajusta os papeis flow_borda e flow_analise (idempotente)
python -m flow_db migrar        # aplica as migracoes pendentes (idempotente, com trava)
python -m flow_db status        # versao aplicada x disponivel
python -m borda                       # a borda (.env.borda); recusa subir se o banco faltar ou estiver defasado
python -m analise                     # a análise (.env.analise), em outro terminal
# ou: scripts/preparar.sh e scripts/tudo.sh (ver INSTRUCOES.md)
```

Senhas dos papéis: só letras, números, `_` e `-`, mínimo 16 (`python -c "import secrets; print(secrets.token_urlsafe(24))"`).

## Quem escreve onde (fonte única: `flow_db/permissoes.py`, espelhada na migração 0005)

| Tabela | borda | análise |
|---|---|---|
| leituras, respostas_dispositivo, entregas_push | grava | lê |
| comandos_push, alertas, confirmacoes, episodios, estado_dispositivo | lê | grava |
| cursor_borda / cursor_analise | grava / lê | — / grava |
| telemetria (legado), auditoria, contas, sessoes | grava | — |

Nenhum papel apaga em tabela de fronteira (só `sessoes`). A retenção é feita pelo TimescaleDB (administrativo).

## Dupla escrita (transição; sai na parte 2)

Por padrão a borda e a análise gravam **só o banco**. `FLOW_GRAVAR_ARQUIVOS=1` liga também os arquivos de antes (alertas e confirmações pela análise, depois do commit; telemetria agregada e auditoria pela borda);
`FLOW_GRAVAR_TELEMETRIA_LEGADO=0` para a tabela `telemetria`. As leituras (`leituras`) vão por uma fila limitada
(`const.MAX_FILA_LEITURAS`) e são gravadas em lote pela thread de descarga; se o banco cair, o servidor segue
aceitando dados e as mais antigas saem ao estourar o teto.

## Leitura da análise: lacunas de id e ciclo atômico

- **Lacunas de id** (`flow_db/_lacunas.py`): colunas `IDENTITY` reservam o id no `INSERT`, não no `COMMIT`; por isso pode haver commit fora de ordem e buracos permanentes (`ROLLBACK`). A análise deve ler com `ler_leituras_seguras` / `ler_respostas_seguras` / `ler_entregas_seguras` (e não com `*_novas`): um buraco cuja linha seguinte tem menos de `config.ESPERA_LACUNA_S` (2 s) faz a leitura parar antes dele; passado o prazo o buraco é tratado como permanente e somado em `cursor_analise.metricas.lacunas_ignoradas`. Com `instante_minimo`, linhas antigas filtradas também aparecem como buraco (só atrasam até o prazo e entram na contagem).
- **Ciclo** (`flow_db/ciclo_analise.py`): `processar_lote(repo, processar)` lê o lote, chama `processar(tx, linhas)` e avança o cursor numa única transação. Exceção em `processar` desfaz saídas e cursor; o lote é reprocessado (at-least-once), então `processar` deve ser idempotente por `episodio_id`/`message_id`. Sem leituras novas, só renova o batimento.
- **Limite de instante na análise:** o laço chama `processar_ciclo(..., janela_cursor_s=JANELA_CONSULTA_CURSOR_S)` (`analise/const.py`, 1 h). O limite é `cursor.ultimo_leitura_instante - janela` (ancorado no cursor, não no relógio), então o banco descarta as partições mais antigas que o cursor e nada legítimo se perde, nem depois de uma parada longa. Só uma linha com id acima do cursor e instante mais de 1 h anterior ao do cursor (buffer da borda descarregado após horas de banco fora) fica de fora e entra em `lacunas_ignoradas`.

## Testes

- `pytest` — padrão. Usa o banco falso em memória (`FLOW_DB_FAKE=1`); não precisa de Docker.
- `pytest -m banco` — contrato contra o PostgreSQL real, em um banco descartável `flow_teste` (recriado a cada sessão).
  Exige o container no ar e as 3 senhas no `.env`.

## Backup e restauração (rodar a partir de `SensorServer/`, com o container `flow-db` no ar)

Backup (formato customizado, só o banco `flow`; use o usuário administrativo, `flow` por padrão):

```bash
docker compose exec -T db pg_dump -U flow -Fc flow > backup_flow.dump
```

Restauração em um banco **novo** (serve também de teste: não toca no `flow`). O TimescaleDB exige o par
`timescaledb_pre_restore()` / `timescaledb_post_restore()` em volta do `pg_restore`:

```bash
docker compose exec -T db psql -U flow -d postgres -c "CREATE DATABASE flow_restaurado"
docker compose exec -T db psql -U flow -d flow_restaurado -c "CREATE EXTENSION IF NOT EXISTS timescaledb; SELECT timescaledb_pre_restore();"
docker compose exec -T db pg_restore -U flow -d flow_restaurado --no-owner < backup_flow.dump
docker compose exec -T db psql -U flow -d flow_restaurado -c "SELECT timescaledb_post_restore();"
```

Conferência: as contagens de `leituras`, `alertas`, `episodios` e a última linha de `schema_migrations` em `flow_restaurado`
devem ser iguais às de `flow` na hora do backup. Para descartar o teste: `DROP DATABASE flow_restaurado WITH (FORCE)`.
Os papéis `flow_borda` e `flow_analise` são do cluster e continuam existindo; se a restauração for em outro cluster, rode
antes `python -m flow_db provisionar`. **Registre a data do último teste de restauração** (checklist, seção 7) e o tempo gasto
(seção 8). Os comandos acima ainda não foram executados: o primeiro teste confirma o procedimento.

## Verificações com o sistema em uso (SQL, via `docker compose exec -T db psql -U flow -d flow`)

```sql
-- sessões por papel (o application_name é flow-flow_borda / flow-flow_analise) e transações ociosas
SELECT usename, application_name, state, count(*) FROM pg_stat_activity
 WHERE usename IN ('flow_borda', 'flow_analise') GROUP BY 1, 2, 3 ORDER BY 1, 3;

-- cursores: batimento recente, atraso e linhas mortas (a análise renova o batimento a cada ciclo de 0,25 s)
SELECT * FROM saude_pipeline;
SELECT relname, n_live_tup, n_dead_tup, last_autovacuum FROM pg_stat_user_tables
 WHERE relname IN ('cursor_analise', 'cursor_borda');

-- tarefas de compressão/retenção: última execução e erros
SELECT hypertable_name, job_id, last_run_status, last_successful_finish, total_failures
  FROM timescaledb_information.job_stats ORDER BY 1;
SELECT * FROM timescaledb_information.job_errors ORDER BY start_time DESC LIMIT 10;

-- tamanho em disco (registre na seção 8 da checklist)
SELECT hypertable_name, pg_size_pretty(hypertable_size(format('%I', hypertable_name)::regclass))
  FROM timescaledb_information.hypertables;
SELECT pg_size_pretty(pg_database_size(current_database()));

-- plano da consulta por cursor: deve citar poucos chunks (os antigos são excluídos pelo limite de instante)
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM leituras
 WHERE id > 0 AND instante_ingestao >= now() - interval '1 hour' ORDER BY id LIMIT 500;
```

Erros de permissão e consultas lentas aparecem no log do container: `docker logs flow-db 2>&1 | grep -i "permission denied"`.

## O que a parte 2 ainda vai mudar

- **Retenção e backup:** política de retenção das tabelas de fronteira (hoje feita só pelo TimescaleDB, administrativo; a `auditoria` ainda não tem) e rotina **automática** de backup (o procedimento manual está acima).
- **Tabela `telemetria` (legado):** removida, junto com `FLOW_GRAVAR_TELEMETRIA_LEGADO`; `leituras` é a fonte.
- **Arquivos de transição:** `FLOW_GRAVAR_ARQUIVOS` e `dataUsers/` saem (agregado, confirmações, alertas, auditoria).
- **Contas e sessões:** `contas.json`/`sessoes.json` continuam sendo a fonte de autenticação (as tabelas `contas`/`sessoes` já existem e têm permissão, mas `borda/auth.py` ainda não as usa).

O banco é criado do zero (não há migração dos `.json` antigos).
