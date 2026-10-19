"""
Contrato contra PostgreSQL + TimescaleDB REAIS. Fora da execucao padrao: `pytest -m banco`.

Usa um banco DESCARTAVEL (FLOW_DB_TESTE_NOME, padrao flow_teste; o nome precisa conter "teste"), recriado a cada
sessao. Nunca toca no banco "flow". Pre-requisitos: container de pe (docker compose up -d) e, no .env ou no ambiente,
FLOW_DB_PASSWORD, FLOW_DB_BORDA_PASSWORD e FLOW_DB_ANALISE_PASSWORD. Os papeis sao do CLUSTER: o teste reaplica as
mesmas senhas do .env (nao as troca).
"""
import os
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
try:
    from dotenv import load_dotenv
    load_dotenv(RAIZ / ".env", override=False, interpolate=False, encoding="utf-8-sig")
except ImportError:  # pragma: no cover
    pass

NOME_TESTE = os.environ.get("FLOW_DB_TESTE_NOME", "flow_teste")
TABELAS_LIMPAVEIS = ("leituras, respostas_dispositivo, entregas_push, comandos_push, alertas, confirmacoes, "
                     "episodios, estado_dispositivo, telemetria, auditoria")


def pytest_collection_modifyitems(items):
    for item in items:
        if "banco" in item.path.parts:
            item.add_marker("banco")


@pytest.fixture(scope="session")
def banco_pg():
    """Cria o banco de teste, provisiona os papeis e aplica as migracoes. Pula tudo se nao houver banco."""
    import psycopg
    from psycopg import sql
    from flow_db import config, migracoes, provisionar

    if "teste" not in NOME_TESTE:
        pytest.fail(f"FLOW_DB_TESTE_NOME={NOME_TESTE!r} precisa conter 'teste' (protege o banco real).")
    faltam = [v for v in ("FLOW_DB_PASSWORD", "FLOW_DB_BORDA_PASSWORD", "FLOW_DB_ANALISE_PASSWORD")
              if not os.environ.get(v)]
    if faltam:
        pytest.skip(f"variaveis ausentes: {', '.join(faltam)}")

    mp = pytest.MonkeyPatch()
    mp.setenv("FLOW_DB_NAME", "postgres")
    try:
        try:
            with psycopg.connect(config.dsn_admin(), autocommit=True) as conn:
                conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(NOME_TESTE)))
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(NOME_TESTE)))
        except psycopg.OperationalError as e:
            pytest.skip(f"banco indisponivel: {str(e)[:150]}")
        mp.setenv("FLOW_DB_NAME", NOME_TESTE)
        try:
            provisionar.provisionar()
        except config.ConfigBanco as e:
            pytest.skip(str(e))
        migracoes.aplicar()
        yield NOME_TESTE
    finally:
        mp.setenv("FLOW_DB_NAME", "postgres")
        try:
            with psycopg.connect(config.dsn_admin(), autocommit=True) as conn:
                conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(NOME_TESTE)))
        except Exception:  # noqa: BLE001
            pass
        mp.undo()


@pytest.fixture
def admin(banco_pg):
    import psycopg
    from flow_db import config
    with psycopg.connect(config.dsn_admin(), autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        conn.execute(f"TRUNCATE {TABELAS_LIMPAVEIS} RESTART IDENTITY")
        conn.execute("UPDATE cursor_analise SET ultimo_leitura_id = 0, ultimo_leitura_instante = NULL,"
                     " ultimo_resposta_id = 0, ultimo_entrega_id = 0, atualizado_em = now(), metricas = '{}'")
        conn.execute("UPDATE cursor_borda SET ultimo_comando_id = 0, ultimo_comando_instante = NULL, atualizado_em = now()")
        yield conn


@pytest.fixture(scope="session")
def pools(banco_pg):
    from flow_db import config, conexao
    pb, pa = conexao.criar_pool(config.PAPEL_BORDA), conexao.criar_pool(config.PAPEL_ANALISE)
    yield pb, pa
    pb.close()
    pa.close()


@pytest.fixture
def borda(pools, admin):
    from flow_db.pg_borda import PgBorda
    return PgBorda(pools[0])


@pytest.fixture
def analise(pools, admin):
    from flow_db.pg_analise import PgAnalise
    return PgAnalise(pools[1])
