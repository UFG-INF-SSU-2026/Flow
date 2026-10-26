"""Corpo de requisicao acima de const_borda.MAX_CONTENT_LENGTH: 413 e sem processar o conteudo."""
import time

import pytest

from borda import const as const_borda
from borda import server

LIMITE_TESTE = 2048


@pytest.fixture
def limite_pequeno(monkeypatch):
    monkeypatch.setitem(server.app.config, "MAX_CONTENT_LENGTH", LIMITE_TESTE)


def _dados(extra: str = "") -> dict:
    return {"source": "app", "user_id": 1, "device_id": "cel1", "timestamp_envio": int(time.time() * 1000),
            "acelerometro": {"magnitude": 9.8, "magnitude_max_janela": 9.9}, "extra": extra}


def test_o_app_usa_o_limite_da_const():
    assert server.app.config["MAX_CONTENT_LENGTH"] == const_borda.MAX_CONTENT_LENGTH


def test_corpo_legitimo_cabe_no_limite_da_const(cli, tokens):
    assert cli.post("/dados", _dados(), tokens["idoso"]).status_code == 200


@pytest.mark.parametrize("rota", ["/dados", "/eventos", "/auth/login", "/auth/refresh"])
def test_corpo_grande_da_413(cli, tokens, limite_pequeno, rota):
    r = cli.post(rota, _dados("x" * (LIMITE_TESTE * 2)), tokens["idoso"])
    assert r.status_code == 413
    assert r.get_json() == {"status": "erro", "codigo": "corpo_grande"}


def test_corpo_abaixo_do_limite_passa(cli, tokens, limite_pequeno):
    assert cli.post("/dados", _dados("x" * 100), tokens["idoso"]).status_code == 200


def test_corpo_grande_nao_e_processado(cli, tokens, limite_pequeno):
    cli.post("/dados", _dados("x" * (LIMITE_TESTE * 2)), tokens["idoso"])
    assert server._fila_leituras == []


def test_413_vai_para_o_log_sem_o_conteudo(cli, tokens, limite_pequeno, capsys):
    cli.post("/dados", _dados("segredo-no-corpo" * 400), tokens["idoso"])
    saida = capsys.readouterr().out
    assert "negado 413 corpo_grande" in saida and "segredo-no-corpo" not in saida
