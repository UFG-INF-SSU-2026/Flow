# Banco de dados (PostgreSQL + TimescaleDB)

Só a **borda** (Servidor 1) fala com o banco, por uma única interface (`flow_db/repo.py`, implementada em `flow_db/pg.py`; o `BancoFalso` em memória a imita para testes e para `FLOW_DB_FAKE=1`). A análise (Servidor 2) não tem banco nem credencial dele. Um único usuário de banco, o mesmo que o `docker-compose.yml` cria. **A conexão é sempre TLS:** o PostgreSQL sobe com o certificado único do projeto (`certs/`) e a borda conecta com `sslmode=verify-full` (ou `verify-ca`, em `FLOW_DB_SSLMODE`), confiando só nesse certificado; o IP ou nome de `FLOW_DB_HOST` precisa estar nele. O banco é o PostgreSQL 17 na imagem `timescale/timescaledb`, e **só a tabela `leituras` é uma hypertable do TimescaleDB** (particionada pelo tempo, em `instante_ingestao`); todas as outras são tabelas comuns do PostgreSQL. Não há compressão, política de retenção nem agregados contínuos.

## Subir do zero (a partir de `SensorServer/`)

```
cp .env.example .env     # preencha FLOW_JWT_SECRET, FLOW_ANALISE_TOKEN, FLOW_DB_PASSWORD
pip install -r requirements.txt
python scripts/gerar_certificado.py    # certificado TLS (o container o carrega ao subir)
docker compose up -d --wait
python -m flow_db migrar        # aplica as migrações pendentes (idempotente, com trava)
python -m flow_db status        # versão aplicada x disponível
python -m borda                 # a borda; recusa subir se o banco faltar ou estiver defasado
python -m analise               # a análise, em outro terminal
# ou: scripts/preparar.sh e scripts/tudo.sh (ver INSTRUCOES.md)
```

Sem Docker, para desenvolver: `FLOW_DB_FAKE=1 python -m borda.server` (banco em memória; os dados somem ao parar).

**Certificado recriado** (`python scripts/gerar_certificado.py --forcar`): `docker compose restart db`. O TLS não exige recriar o volume. Sem `certs/flow.crt` e `certs/flow.key` o container não sobe, e sem o certificado a borda não conecta.

**Banco antigo:** a migração `0001_base.sql` cria a extensão `timescaledb` e converte `leituras` em hypertable, e isso exige a imagem do TimescaleDB. Um volume criado antes, com a imagem `postgres:17` simples (ou com o esquema anterior), não serve: recrie com `docker compose down -v` e suba de novo (`scripts/preparar.sh`). Os dados de teste não são migrados, **inclusive as contas**: depois do `down -v`, recrie-as (`python -m borda.auth criar-conta`, ver `INSTRUCOES.md`).

## Tabelas

| Tabela | Conteúdo |
|---|---|
| `leituras` | **Hypertable** (por `instante_ingestao`). Uma linha por leitura aceita (fonte, `conta_id` do token, `payload` em jsonb). A chave primária é `(id, instante_ingestao)`: o TimescaleDB exige a coluna de tempo em toda chave única. |
| `alertas` | Alertas das regras (luz inadequada, imobilidade). |
| `confirmacoes` | Registros do ciclo de confirmação (`episodio.aberto`, `push.ack`, `confirmacao.resposta`, `episodio.encerrado`). |
| `episodios` | Um episódio de queda por linha, com o estado e o motivo do encerramento. |
| `comandos_push` | Histórico das mensagens de push (`message_id` único). É daqui que a restauração tira os `messageId` de cada episódio. |
| `estado_dispositivo` | Resumo da imobilidade por dispositivo (`GET /estado/<device_id>`), gravado só quando muda. |
| `contas` | Contas de login (`id`, `papel`, `user_ids`, `fontes`, hash scrypt da senha, `criada_em`). Criadas pela CLI `python -m borda.auth criar-conta`, que exige o banco no ar. |
| `sessoes` | Refresh tokens em uso (só o hash do token, a conta, a fonte, o `device_id` e `expira_em`). Uso único: `DELETE ... RETURNING`. Sem chave estrangeira para `contas`: remover a conta faz o refresh recusar. As vencidas são podadas a cada login ou refresh. |

Quem grava é sempre a borda: as leituras em lote (uma só thread) e, numa transação, as decisões da análise (`borda/efeitos.py`). Se o banco cai, as leituras ficam numa fila limitada em memória (`MAX_FILA_LEITURAS`) e nenhuma decisão é aplicada nem push enviado até ele voltar. Contas e sessões também estão no banco (tabelas comuns): não há arquivo de dados em disco. Com o banco fora, só login e refresh falham (`503`); o access token é conferido sem banco e quem já está logado segue enviando.

## Testes

- `pytest` — padrão. Usa o banco falso em memória; não precisa de Docker. O contrato do `Repo` (`tests/_contrato_repo.py`) roda aqui.
- `pytest -m banco` — o mesmo contrato contra o PostgreSQL real, num banco descartável `flow_teste` (recriado a cada sessão), por TLS. Exige o container no ar, `FLOW_DB_PASSWORD` no `.env` e o certificado gerado (`certs/`).

## Backup e restauração (a partir de `SensorServer/`, com o container `flow-db` no ar)

Backup (formato customizado, só o banco `flow`):

```bash
docker compose exec -T db pg_dump -U flow -Fc flow > backup_flow.dump
```

Restauração em um banco **novo** (serve também de teste: não toca no `flow`). Com TimescaleDB, a restauração fica entre `timescaledb_pre_restore()` e `timescaledb_post_restore()`:

```bash
docker compose exec -T db psql -U flow -d postgres -c "CREATE DATABASE flow_restaurado"
docker compose exec -T db psql -U flow -d flow_restaurado -c "SELECT timescaledb_pre_restore()"
docker compose exec -T db pg_restore -U flow -d flow_restaurado --no-owner < backup_flow.dump
docker compose exec -T db psql -U flow -d flow_restaurado -c "SELECT timescaledb_post_restore()"
```

Conferência: as contagens de `leituras`, `alertas`, `episodios` e a última linha de `schema_migrations` em `flow_restaurado` devem ser iguais às de `flow` na hora do backup. Para descartar: `DROP DATABASE flow_restaurado WITH (FORCE)`. Estes comandos ainda não foram executados neste repositório: o primeiro teste confirma o procedimento; registre a data.

## Verificações com o sistema em uso (SQL, via `docker compose exec -T db psql -U flow -d flow`)

```sql
-- conexões da aplicação
SELECT application_name, state, count(*) FROM pg_stat_activity WHERE datname = 'flow' GROUP BY 1, 2;

-- hypertables (deve listar só `leituras`) e seus pedaços (chunks)
SELECT hypertable_name, num_chunks, compression_enabled FROM timescaledb_information.hypertables;
SELECT chunk_name, range_start, range_end FROM timescaledb_information.chunks WHERE hypertable_name = 'leituras' ORDER BY range_start;

-- contas cadastradas (o hash da senha nao e listado)
SELECT id, papel, user_ids, fontes FROM contas;

-- tamanho em disco (para `leituras`, hypertable_size soma todos os chunks; pg_total_relation_size só veria a tabela-mãe)
SELECT pg_size_pretty(hypertable_size('leituras')) AS leituras;
SELECT relname, pg_size_pretty(pg_total_relation_size(oid)) FROM pg_class
 WHERE relname IN ('alertas', 'confirmacoes', 'episodios', 'comandos_push', 'estado_dispositivo');
SELECT pg_size_pretty(pg_database_size(current_database()));

-- conexões da aplicação cifradas (ssl = t)
SELECT a.application_name, s.ssl, s.version FROM pg_stat_ssl s JOIN pg_stat_activity a USING (pid) WHERE a.application_name = 'flow';

-- leituras mais recentes
SELECT id, instante_ingestao, user_id, fonte FROM leituras ORDER BY id DESC LIMIT 10;
```

## Pendências

- **Retenção e compressão:** `leituras` cresce sem limite; compressão, política de retenção (`drop_chunks`) e agregados contínuos do TimescaleDB não estão configurados e ficam para quando o volume pedir. Backup automático também não existe (o procedimento manual está acima).
