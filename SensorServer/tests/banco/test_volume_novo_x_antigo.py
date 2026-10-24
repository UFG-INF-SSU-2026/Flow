"""Volume vazio x volume antigo (checklist 3.1 / 4.1 / 7): o esquema final tem de ser o mesmo.

"Antigo" = banco que so tinha a 0001 (o estado de C0) e ja guardava dados; "novo" = banco vazio com todas as
migracoes. Os dois sao bancos descartaveis proprios (nome com "teste"); o banco de teste principal nao e tocado.
"""
import os
import shutil

import psycopg
import pytest
from psycopg import sql

from flow_db import config, migracoes, provisionar

pytestmark = pytest.mark.banco

BASE = os.environ.get("FLOW_DB_TESTE_NOME", "flow_teste")
NOVO, ANTIGO = f"{BASE}_vnovo", f"{BASE}_vantigo"


def _recriar(monkeypatch, nome):
    monkeypatch.setenv("FLOW_DB_NAME", "postgres")
    with psycopg.connect(config.dsn_admin(), autocommit=True) as c:
        c.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(nome)))
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(nome)))
    monkeypatch.setenv("FLOW_DB_NAME", nome)
    provisionar.provisionar()


def _descartar(monkeypatch, *nomes):
    monkeypatch.setenv("FLOW_DB_NAME", "postgres")
    with psycopg.connect(config.dsn_admin(), autocommit=True) as c:
        for nome in nomes:
            c.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(nome)))


def _retrato(monkeypatch, nome):
    """Esquema observavel do banco `nome`: colunas, indices, hypertables, politicas, grants e migracoes."""
    monkeypatch.setenv("FLOW_DB_NAME", nome)
    consultas = {
        "colunas": "SELECT table_name, column_name, data_type, is_nullable, column_default, is_identity"
                   " FROM information_schema.columns WHERE table_schema = 'public' ORDER BY 1, 2",
        "indices": "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' ORDER BY 1, 2",
        "hypertables": "SELECT hypertable_name, compression_enabled FROM timescaledb_information.hypertables ORDER BY 1",
        "politicas": "SELECT hypertable_name, proc_name, schedule_interval::text, config->>'drop_after',"
                     " config->>'compress_after' FROM timescaledb_information.jobs"
                     " WHERE proc_name IN ('policy_retention', 'policy_compression') ORDER BY 1, 2",
        "grants": "SELECT grantee, table_name, privilege_type FROM information_schema.role_table_grants"
                  " WHERE table_schema = 'public' AND grantee = ANY(%s) ORDER BY 1, 2, 3",
        "migracoes": "SELECT versao, nome FROM schema_migrations ORDER BY 1",
    }
    with psycopg.connect(config.dsn_admin(), autocommit=True) as c:
        return {k: c.execute(q, (list(config.PAPEIS),) if k == "grants" else None).fetchall()
                for k, q in consultas.items()}


def test_volume_antigo_migrado_fica_igual_ao_volume_novo(banco_pg, monkeypatch, tmp_path):
    try:
        # volume novo: banco vazio, todas as migracoes
        _recriar(monkeypatch, NOVO)
        migracoes.aplicar()

        # volume antigo: so a 0001, com dados; depois as migracoes pendentes
        _recriar(monkeypatch, ANTIGO)
        so_a_primeira = tmp_path / "so_0001"
        so_a_primeira.mkdir()
        shutil.copy(migracoes.listar()[0].caminho, so_a_primeira)
        assert migracoes.aplicar(diretorio=so_a_primeira) == [1]
        with psycopg.connect(config.dsn_admin(), autocommit=True) as c:
            c.execute("INSERT INTO telemetria (instante, user_id, app, wokwi) VALUES (now(), 7, '{\"a\": 1}', NULL)")
            c.execute("INSERT INTO auditoria (rota, status, motivo) VALUES ('/x', 401, 'sem_token')")
            c.execute("INSERT INTO alertas (user_id, tipo, mensagem) VALUES (7, 'queda', 'antigo')")
            c.execute("INSERT INTO confirmacoes (user_id, tipo) VALUES (7, 'ok')")
        assert migracoes.aplicar() == list(range(2, migracoes.ultima_disponivel() + 1))

        # nenhuma linha antiga se perdeu, e as colunas novas deixaram as antigas legiveis
        with psycopg.connect(config.dsn_admin(), autocommit=True) as c:
            for tabela in ("telemetria", "auditoria", "alertas", "confirmacoes"):
                assert c.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(tabela))).fetchone()[0] == 1
            assert c.execute("SELECT mensagem FROM alertas WHERE id IS NOT NULL").fetchone()[0] == "antigo"
            assert c.execute("SELECT count(*) FROM confirmacoes WHERE id IS NOT NULL").fetchone()[0] == 1

        assert _retrato(monkeypatch, ANTIGO) == _retrato(monkeypatch, NOVO)
    finally:
        _descartar(monkeypatch, NOVO, ANTIGO)
