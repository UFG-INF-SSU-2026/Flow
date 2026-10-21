# Banco de dados (PostgreSQL + TimescaleDB) — parte 1

Estado desta entrega: camada de acesso, esquema, permissões e integração do servidor atual (dupla escrita).
A separação em dois serviços e o fim dos arquivos ficam para a parte 2.

## Subir do zero (a partir de `SensorServer/`)

```
cp .env.example .env     # preencha FLOW_JWT_SECRET, FLOW_DB_PASSWORD, FLOW_DB_BORDA_PASSWORD, FLOW_DB_ANALISE_PASSWORD
docker compose up -d     # espere ficar healthy
pip install -r requirements.txt
python -m flow_db provisionar   # cria/ajusta os papeis flow_borda e flow_analise (idempotente)
python -m flow_db migrar        # aplica as migracoes pendentes (idempotente, com trava)
python -m flow_db status        # versao aplicada x disponivel
python -m borda.server                # recusa subir se o banco faltar ou estiver defasado
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

## Dupla escrita (transição)

Por padrão o servidor grava o banco **e** os arquivos de hoje. `FLOW_GRAVAR_ARQUIVOS=0` desliga os arquivos;
`FLOW_GRAVAR_TELEMETRIA_LEGADO=0` para a tabela `telemetria`. As leituras (`leituras`) vão por uma fila limitada
(`const.MAX_FILA_LEITURAS`) e são gravadas em lote pela thread de descarga; se o banco cair, o servidor segue
aceitando dados e as mais antigas saem ao estourar o teto.

## Leitura da análise: lacunas de id e ciclo atômico

- **Lacunas de id** (`flow_db/_lacunas.py`): colunas `IDENTITY` reservam o id no `INSERT`, não no `COMMIT`; por isso pode haver commit fora de ordem e buracos permanentes (`ROLLBACK`). A análise deve ler com `ler_leituras_seguras` / `ler_respostas_seguras` / `ler_entregas_seguras` (e não com `*_novas`): um buraco cuja linha seguinte tem menos de `config.ESPERA_LACUNA_S` (2 s) faz a leitura parar antes dele; passado o prazo o buraco é tratado como permanente e somado em `cursor_analise.metricas.lacunas_ignoradas`. Com `instante_minimo`, linhas antigas filtradas também aparecem como buraco (só atrasam até o prazo e entram na contagem).
- **Ciclo** (`flow_db/ciclo_analise.py`): `processar_lote(repo, processar)` lê o lote, chama `processar(tx, linhas)` e avança o cursor numa única transação. Exceção em `processar` desfaz saídas e cursor; o lote é reprocessado (at-least-once), então `processar` deve ser idempotente por `episodio_id`/`message_id`. Sem leituras novas, só renova o batimento.

## Testes

- `pytest` — padrão. Usa o banco falso em memória (`FLOW_DB_FAKE=1`); não precisa de Docker.
- `pytest -m banco` — contrato contra o PostgreSQL real, em um banco descartável `flow_teste` (recriado a cada sessão).
  Exige o container no ar e as 3 senhas no `.env`.

## Fora desta parte

`contas.json`/`sessoes.json` continuam sendo a fonte de autenticação (as tabelas `contas`/`sessoes` já existem e têm
permissão, mas o `borda/auth.py` ainda não as usa). Processo de análise separado, consumo por cursor e `/health` com
`saude_pipeline` (a visão e `RepoBorda.ler_saude()` já estão prontos).
