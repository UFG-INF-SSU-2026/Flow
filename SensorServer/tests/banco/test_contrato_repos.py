"""O contrato do Repo contra o PostgreSQL real (a mesma bateria roda no banco falso em tests/unit/test_repo_contrato.py)."""
import pytest

import _contrato_repo as contrato

pytestmark = pytest.mark.banco


@pytest.mark.parametrize("caso", contrato.TODOS, ids=lambda f: f.__name__)
def test_contrato(repo, caso):
    caso(repo)


def test_lote_com_linha_invalida_nao_grava_nenhuma(repo):
    import psycopg
    contrato.lote_com_linha_invalida_nao_grava_nenhuma(repo, psycopg.errors.CheckViolation)


def test_versao_do_esquema_atende_ao_que_o_codigo_exige(pool):
    from flow_db import conexao, versao
    assert conexao.verificar_versao_minima(pool, versao.MIGRACAO_MINIMA) >= versao.MIGRACAO_MINIMA
