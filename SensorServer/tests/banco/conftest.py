"""
Contrato contra PostgreSQL REAL. Fora da execucao padrao: `pytest -m banco`.

Usa um banco DESCARTAVEL (FLOW_DB_TESTE_NOME, padrao flow_teste; o nome precisa conter "teste"), recriado a cada sessao.
Nunca toca no banco "flow". Pre-requisitos: container de pe (docker compose up -d) e FLOW_DB_PASSWORD no .env ou no ambiente.
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


def pytest_collection_modifyitems(items):
    for item in items:
        if "banco" in item.path.parts:
            item.add_marker("banco")


@pytest.fixture(scope="session")
def banco_pg():
    """Cria o banco de teste e aplica as migracoes. Pula tudo se nao houver banco."""
    import psycopg
    from psycopg import sql
    from flow_db import config, migracoes

    if "teste" not in NOME_TESTE:
        pytest.fail(f"FLOW_DB_TESTE_NOME={NOME_TESTE!r} precisa conter 'teste' (protege o banco real).")
    if not os.environ.get("FLOW_DB_PASSWORD"):
        pytest.skip("variavel ausente: FLOW_DB_PASSWORD")

    mp = pytest.MonkeyPatch()
    mp.setenv("FLOW_DB_NAME", "postgres")
    try:
        try:
            with psycopg.connect(config.dsn(), autocommit=True) as conn:
                conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(NOME_TESTE)))
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(NOME_TESTE)))
        except psycopg.OperationalError as e:
            pytest.skip(f"banco indisponivel: {str(e)[:150]}")
        mp.setenv("FLOW_DB_NAME", NOME_TESTE)
        migracoes.aplicar()
        yield NOME_TESTE
    finally:
        mp.setenv("FLOW_DB_NAME", "postgres")
        try:
            with psycopg.connect(config.dsn(), autocommit=True) as conn:
                conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(NOME_TESTE)))
        except Exception:  # noqa: BLE001
            pass
        mp.undo()


@pytest.fixture(scope="session")
def pool(banco_pg):
    from flow_db import conexao
    p = conexao.criar_pool()
    yield p
    p.close()


@pytest.fixture
def repo(pool):
    """O Repo sobre o banco real, com as tabelas vazias."""
    from flow_db.pg import Pg
    with pool.connection() as conn:
        conn.execute("TRUNCATE leituras, comandos_push, alertas, confirmacoes, episodios, estado_dispositivo RESTART IDENTITY")
    return Pg(pool)
