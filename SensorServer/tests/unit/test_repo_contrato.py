"""O contrato do Repo contra o banco falso (a mesma bateria roda no PostgreSQL real em tests/banco/)."""
import pytest

import _contrato_repo as contrato
from flow_db.fake import BancoFalso


@pytest.mark.parametrize("caso", contrato.TODOS, ids=lambda f: f.__name__)
def test_contrato(caso):
    caso(BancoFalso())


def test_lote_com_linha_invalida_nao_grava_nenhuma():
    contrato.lote_com_linha_invalida_nao_grava_nenhuma(BancoFalso(), ValueError)
