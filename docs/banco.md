# Banco de dados (PostgreSQL + TimescaleDB)

Só a **borda** (Servidor 1) fala com o banco, por uma única interface (`flow_db/repo.py`, implementada em `flow_db/pg.py`; o `BancoFalso` em memória a imita para testes e para `FLOW_DB_FAKE=1`). A análise (Servidor 2) não tem banco nem credencial dele. **Dois papéis de banco:** o **administrativo** (`FLOW_DB_ADMIN_USER`/`FLOW_DB_ADMIN_PASSWORD`, o superusuário que o `docker-compose.yml` cria e dono do esquema), usado só pelo `docker compose`, por `python -m flow_db` (migração e status) e pelo backup; e o da **borda**, `flow_borda` (`FLOW_DB_USER`/`FLOW_DB_PASSWORD`), que só faz `SELECT`, `INSERT`, `UPDATE` e `DELETE` nas tabelas do app, sem DDL (em `schema_migrations` só lê, para conferir a versão na subida). `python -m flow_db migrar` aplica as migrações com o papel administrativo e **cria `flow_borda`** (ou atualiza a senha, se já existe) com os `GRANT` e o `ALTER DEFAULT PRIVILEGES` das tabelas futuras; a senha vem de `FLOW_DB_PASSWORD` e nunca é escrita em SQL versionado. As variáveis `FLOW_DB_ADMIN_*` do `.env` só entram no ambiente de `python -m flow_db`: a borda, o modo de desenvolvimento e a CLI de contas nunca as recebem. **A conexão é sempre TLS:** o PostgreSQL sobe com o certificado único do projeto (`certs/`) e a borda conecta com `sslmode=verify-full` (ou `verify-ca`, em `FLOW_DB_SSLMODE`), confiando só nesse certificado; o IP ou nome de `FLOW_DB_HOST` precisa estar nele. O banco é o PostgreSQL 17 na imagem `timescale/timescaledb`, e **só a tabela `leituras` é uma hypertable do TimescaleDB** (particionada pelo tempo, em `instante_ingestao`); todas as outras são tabelas comuns do PostgreSQL. Há retenção (seção abaixo); não há compressão nem agregados contínuos.

## Subir do zero (a partir de `SensorServer/`)

```
cp .env.example .env     # preencha FLOW_JWT_SECRET, FLOW_ANALISE_TOKEN, FLOW_DB_ADMIN_PASSWORD, FLOW_DB_PASSWORD
pip install -r requirements.txt
python scripts/gerar_certificado.py    # CA e certificados TLS (o container carrega o do banco ao subir)
docker compose up -d --wait
python -m flow_db migrar        # aplica as migrações pendentes e garante o papel flow_borda (idempotente, com trava)
python -m flow_db status        # versão aplicada x disponível
python -m borda                 # a borda; recusa subir se o banco faltar ou estiver defasado
python -m analise               # a análise, em outro terminal
# ou: scripts/preparar.sh e scripts/tudo.sh (ver INSTRUCOES.md)
```

Sem Docker, para desenvolver: `FLOW_DB_FAKE=1 python -m borda.server` (banco em memória; os dados somem ao parar).

**Certificado reemitido** (`python scripts/gerar_certificado.py --forcar`): `docker compose restart db`. O TLS não exige recriar o volume. O banco apresenta o certificado que a CA do projeto emitiu para ele (`certs/banco.crt` e `certs/banco.key`, os únicos arquivos de `certs/` que o container recebe; a chave da CA nunca é montada), e a borda o confere com `sslmode=verify-full`, confiando só em `certs/ca.crt`. Sem `banco.crt` e `banco.key` o container não sobe, e sem `ca.crt` a borda não conecta.

**Banco antigo:** a migração `0001_base.sql` cria a extensão `timescaledb` e converte `leituras` em hypertable, e isso exige a imagem do TimescaleDB. Um volume criado antes, com a imagem `postgres:17` simples (ou com o esquema anterior), não serve: recrie com `docker compose down -v` e suba de novo (`scripts/preparar.sh`). Os dados de teste não são migrados, **inclusive as contas**: depois do `down -v`, recrie-as (`python -m borda.auth criar-conta`, ver `INSTRUCOES.md`).

## Retenção

Os prazos são sempre **maiores que o atraso máximo tolerado** (a restauração da análise olha só os últimos 30 s de leituras e 1 h de episódios).

| Tabela | Prazo | Quem apaga |
|---|---|---|
| `leituras` | 14 dias | Política do TimescaleDB (`add_retention_policy`, migração `0002`), em segundo plano no banco. O apagamento é por pedaço (chunk de 7 dias) e só de pedaço inteiramente mais velho que o prazo: uma leitura fica de 14 a 21 dias. |
| `alertas`, `confirmacoes`, `comandos_push` | 90 dias | Um passo da **própria borda** (`passo_retencao`, thread `flow-retencao`), ao subir e a cada hora, com o papel `flow_borda` (que já tem `DELETE`). É o único trabalho de fundo novo. |
| `episodios` | 90 dias após a última atualização | O mesmo passo, **só os encerrados**: episódio aberto nunca é apagado. |

`estado_dispositivo`, `contas` e `sessoes` não entram: a primeira tem uma linha por dispositivo, as contas são cadastro e as sessões vencidas já são podadas a cada login. Os prazos das tabelas comuns ficam em `borda/const.py` (`RETENCAO_HISTORICO_DIAS`, `INTERVALO_RETENCAO_S`); o de `leituras`, na migração (para mudar, uma migração nova com `remove_retention_policy` e outro `add_retention_policy`). Banco fora do ar: o passo registra uma linha de log (com limite de repetição) e tenta de novo na hora seguinte. Não há compressão, e os resumos contínuos (hora/dia) ficam **fora, de propósito**: nenhuma regra os usa.

## Tabelas

| Tabela | Conteúdo |
|---|---|
| `leituras` | **Hypertable** (por `instante_ingestao`). Uma linha por leitura aceita (fonte, `conta_id` do token, `payload` em jsonb). A chave primária é `(id, instante_ingestao)`: o TimescaleDB exige a coluna de tempo em toda chave única. |
| `alertas` | Alertas das regras (luz inadequada, imobilidade). |
| `confirmacoes` | Registros do ciclo de confirmação (`episodio.aberto`, `push.ack`, `confirmacao.resposta`, `episodio.encerrado`). |
| `episodios` | Um episódio de queda por linha, com o estado e o motivo do encerramento. |
| `comandos_push` | **Histórico** das mensagens de push (`message_id` único) e o **elo** entre a resposta do dispositivo e o episódio: é daqui que a restauração tira os `messageId` de cada episódio ao reiniciar. Não há tabela de entregas: o rastro da entrega é um log estruturado, e o `push.ack` do idoso já fica em `confirmacoes`. |
| `estado_dispositivo` | Resumo da imobilidade por dispositivo (`GET /estado/<device_id>`), gravado só quando muda. |
| `contas` | Contas de login (`id`, `papel`, `user_ids`, `fontes`, hash scrypt da senha, `criada_em`). Criadas pela CLI `python -m borda.auth criar-conta`, que exige o banco no ar. |
| `sessoes` | Refresh tokens em uso (só o hash do token, a conta, a fonte, o `device_id` e `expira_em`). Uso único: `DELETE ... RETURNING`. Sem chave estrangeira para `contas`: remover a conta faz o refresh recusar. As vencidas são podadas a cada login ou refresh. |

Quem grava é sempre a borda: as leituras em lote (uma só thread) e, numa transação, as decisões da análise (`borda/efeitos.py`). Se o banco cai, as leituras ficam numa fila limitada em memória (`MAX_FILA_LEITURAS`) e nenhuma decisão é aplicada nem push enviado até ele voltar. Contas e sessões também estão no banco (tabelas comuns): não há arquivo de dados em disco. Com o banco fora, só login e refresh falham (`503`); o access token é conferido sem banco e quem já está logado segue enviando.

## Testes

- `pytest` — padrão. Usa o banco falso em memória; não precisa de Docker. O contrato do `Repo` (`tests/_contrato_repo.py`) roda aqui.
- `pytest -m banco` — o mesmo contrato contra o PostgreSQL real, num banco descartável `flow_teste` (recriado a cada sessão), por TLS. Exige o container no ar, `FLOW_DB_ADMIN_PASSWORD` e `FLOW_DB_PASSWORD` no `.env` (o administrativo cria o banco, migra e limpa as tabelas; o `Repo` roda como `flow_borda`, sem DDL) e a CA e os certificados gerados (`certs/`).

## Backup e restauração (a partir de `SensorServer/`, com o container `flow-db` no ar)

Os dois usam o papel **administrativo** (`FLOW_DB_ADMIN_USER`, padrão `flow`) e rodam sem agendamento, quando você quiser.

```bash
scripts/backup.sh              # backup em backups/flow_<data>.dump (no Windows: scripts\backup.ps1)
scripts/backup.sh --testar     # idem e, em seguida, restaura num banco descartável, compara as contagens e o apaga
                               # (no Windows: scripts\backup.ps1 -Testar). Pare a borda antes, para a comparação ser exata.
```

`backups/` está no `.gitignore`: o dump tem dados pessoais. Passo a passo manual (o que o script faz):

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

Conferência: as contagens de `leituras`, `alertas`, `episodios` e a última linha de `schema_migrations` em `flow_restaurado` devem ser iguais às de `flow` na hora do backup. Para descartar: `DROP DATABASE flow_restaurado WITH (FORCE)`. Registre a data do primeiro `--testar` bem-sucedido.

**O papel da borda não faz DDL** (conferência negativa, com a credencial da borda, entre processos reais; a senha é a de `FLOW_DB_PASSWORD`). Os dois comandos devem ser **recusados** (`permission denied`):

```bash
docker compose exec -T -e PGPASSWORD="<FLOW_DB_PASSWORD>" db psql -h 127.0.0.1 -U flow_borda -d flow -c "CREATE TABLE teste_ddl (i int)"
docker compose exec -T -e PGPASSWORD="<FLOW_DB_PASSWORD>" db psql -h 127.0.0.1 -U flow_borda -d flow -c "DROP TABLE alertas"
```

## Verificações com o sistema em uso (SQL, via `docker compose exec -T db psql -U flow -d flow`)

```sql
-- conexões da aplicação
SELECT application_name, state, count(*) FROM pg_stat_activity WHERE datname = 'flow' GROUP BY 1, 2;

-- retenção de `leituras` ativa (14 dias; deve listar um job policy_retention)
SELECT job_id, proc_name, schedule_interval, config FROM timescaledb_information.jobs WHERE proc_name = 'policy_retention';

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

- **Compressão e agregados contínuos:** não estão configurados, de propósito (o volume não pede). Se o professor quiser um uso mais forte de série temporal, um agregado horário em `leituras` é o primeiro candidato.
- **Backup automático:** não existe; `scripts/backup.sh` é manual.
