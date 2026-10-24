# Banco de dados (PostgreSQL)

Só a **borda** (Servidor 1) fala com o banco, por uma única interface (`flow_db/repo.py`, implementada em `flow_db/pg.py`; o `BancoFalso` em memória a imita para testes e para `FLOW_DB_FAKE=1`). A análise (Servidor 2) não tem banco nem credencial dele. Um único usuário de banco, o mesmo que o `docker-compose.yml` cria. Tabelas comuns do PostgreSQL: sem particionamento, sem compressão e sem retenção automática.

## Subir do zero (a partir de `SensorServer/`)

```
cp .env.example .env     # preencha FLOW_JWT_SECRET, FLOW_ANALISE_TOKEN, FLOW_DB_PASSWORD
docker compose up -d --wait
pip install -r requirements.txt
python -m flow_db migrar        # aplica as migrações pendentes (idempotente, com trava)
python -m flow_db status        # versão aplicada x disponível
python -m borda                 # a borda; recusa subir se o banco faltar ou estiver defasado
python -m analise               # a análise, em outro terminal
# ou: scripts/preparar.sh e scripts/tudo.sh (ver INSTRUCOES.md)
```

Sem Docker, para desenvolver: `FLOW_DB_FAKE=1 python -m borda.server` (banco em memória; os dados somem ao parar).

**Banco antigo:** o esquema foi reduzido a uma única migração (`0001_base.sql`). Um volume criado antes (com TimescaleDB e papéis separados) não serve: recrie com `docker compose down -v` e suba de novo. Os dados de teste não são migrados.

## Tabelas

| Tabela | Conteúdo |
|---|---|
| `leituras` | Uma linha por leitura aceita (`instante_ingestao`, fonte, `conta_id` do token, `payload` em jsonb). |
| `alertas` | Alertas das regras (luz inadequada, imobilidade). |
| `confirmacoes` | Registros do ciclo de confirmação (`episodio.aberto`, `push.ack`, `confirmacao.resposta`, `episodio.encerrado`). |
| `episodios` | Um episódio de queda por linha, com o estado e o motivo do encerramento. |
| `comandos_push` | Histórico das mensagens de push (`message_id` único). É daqui que a restauração tira os `messageId` de cada episódio. |
| `estado_dispositivo` | Resumo da imobilidade por dispositivo (`GET /estado/<device_id>`), gravado só quando muda. |

Quem grava é sempre a borda: as leituras em lote (uma só thread) e, numa transação, as decisões da análise (`borda/efeitos.py`). Se o banco cai, as leituras ficam numa fila limitada em memória (`MAX_FILA_LEITURAS`) e nenhuma decisão é aplicada nem push enviado até ele voltar. Contas e sessões **não** estão no banco: ficam em `contas.json` e `sessoes.json` (só da borda).

## Testes

- `pytest` — padrão. Usa o banco falso em memória; não precisa de Docker. O contrato do `Repo` (`tests/_contrato_repo.py`) roda aqui.
- `pytest -m banco` — o mesmo contrato contra o PostgreSQL real, num banco descartável `flow_teste` (recriado a cada sessão). Exige o container no ar e `FLOW_DB_PASSWORD` no `.env`.

## Backup e restauração (a partir de `SensorServer/`, com o container `flow-db` no ar)

Backup (formato customizado, só o banco `flow`):

```bash
docker compose exec -T db pg_dump -U flow -Fc flow > backup_flow.dump
```

Restauração em um banco **novo** (serve também de teste: não toca no `flow`):

```bash
docker compose exec -T db psql -U flow -d postgres -c "CREATE DATABASE flow_restaurado"
docker compose exec -T db pg_restore -U flow -d flow_restaurado --no-owner < backup_flow.dump
```

Conferência: as contagens de `leituras`, `alertas`, `episodios` e a última linha de `schema_migrations` em `flow_restaurado` devem ser iguais às de `flow` na hora do backup. Para descartar: `DROP DATABASE flow_restaurado WITH (FORCE)`. Estes comandos ainda não foram executados neste repositório: o primeiro teste confirma o procedimento; registre a data.

## Verificações com o sistema em uso (SQL, via `docker compose exec -T db psql -U flow -d flow`)

```sql
-- conexões da aplicação
SELECT application_name, state, count(*) FROM pg_stat_activity WHERE datname = 'flow' GROUP BY 1, 2;

-- tamanho em disco
SELECT relname, pg_size_pretty(pg_total_relation_size(oid)) FROM pg_class
 WHERE relname IN ('leituras', 'alertas', 'confirmacoes', 'episodios', 'comandos_push', 'estado_dispositivo');
SELECT pg_size_pretty(pg_database_size(current_database()));

-- leituras mais recentes
SELECT id, instante_ingestao, user_id, fonte FROM leituras ORDER BY id DESC LIMIT 10;
```

## Pendências

- **Retenção e compressão:** `leituras` cresce sem limite; apagar as antigas (`DELETE ... WHERE instante_ingestao < ...`) ou particionar fica para quando o volume pedir. Backup automático também não existe (o procedimento manual está acima).
