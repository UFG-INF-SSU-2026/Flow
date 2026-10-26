"""
Contrato contra PostgreSQL REAL. Fora da execucao padrao: `pytest -m banco`.

Usa um banco DESCARTAVEL (FLOW_DB_TESTE_NOME, padrao flow_teste; o nome precisa conter "teste"), recriado a cada sessao.
Nunca toca no banco "flow". Pre-requisitos: container de pe (docker compose up -d) e FLOW_DB_PASSWORD, FLOW_DB_ADMIN_PASSWORD no
.env ou no ambiente. A criacao do banco, a migracao e a limpeza das tabelas usam o papel administrativo; o resto (o Repo) usa o
papel da borda, sem DDL, como em producao.
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
    for variavel in ("FLOW_DB_PASSWORD", "FLOW_DB_ADMIN_PASSWORD"):
        if not os.environ.get(variavel):
            pytest.skip(f"variavel ausente: {variavel}")

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
        migracoes.aplicar()
        migracoes.garantir_papel_borda()
        yield NOME_TESTE
    finally:
        mp.setenv("FLOW_DB_NAME", "postgres")
        try:
            with psycopg.connect(config.dsn_admin(), autocommit=True) as conn:
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
    import psycopg
    from flow_db import config
    from flow_db.pg import Pg
    with psycopg.connect(config.dsn_admin(), autocommit=True) as conn:      # TRUNCATE e DDL: so o papel administrativo
        conn.execute("TRUNCATE leituras, comandos_push, alertas, confirmacoes, episodios, estado_dispositivo, contas, sessoes RESTART IDENTITY")
    return Pg(pool)
