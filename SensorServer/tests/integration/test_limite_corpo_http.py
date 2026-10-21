"""Corpo de requisicao acima de const_borda.MAX_CONTENT_LENGTH: 413, com auditoria e sem processar o conteudo."""
import json
import os
import time

import pytest

from comum import const as const_comum
from borda import const as const_borda
from borda import server

LIMITE_TESTE = 2048


@pytest.fixture
def limite_pequeno(monkeypatch):
    monkeypatch.setitem(server.app.config, "MAX_CONTENT_LENGTH", LIMITE_TESTE)


def _dados(extra: str = "") -> dict:
    return {"source": "app", "user_id": 1, "device_id": "cel1", "timestamp_envio": int(time.time() * 1000),
            "acelerometro": {"magnitude": 9.8, "magnitude_max_janela": 9.9}, "extra": extra}


def _auditoria() -> list[dict]:
    caminho = os.path.join(const_comum.DATA_DIR, "auditoria.jsonl")
    if not os.path.exists(caminho):
        return []
    with open(caminho, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


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
    assert server._buffer == {}


def test_413_entra_na_auditoria_sem_o_conteudo(cli, tokens, limite_pequeno):
    cli.post("/dados", _dados("segredo-no-corpo" * 400), tokens["idoso"])
    linhas = [l for l in _auditoria() if l["status"] == 413]
    assert len(linhas) == 1
    assert linhas[0]["rota"] == "/dados" and linhas[0]["motivo"] == "corpo_grande"
    assert "segredo-no-corpo" not in json.dumps(linhas[0])


def test_sem_token_continua_401_antes_de_ler_o_corpo(cli, limite_pequeno):
    r = cli.post("/dados", _dados("x" * (LIMITE_TESTE * 2)), None)
    assert r.status_code == 401


def test_413_em_servidor_real(servidor_real, tokens, limite_pequeno):
    t = servidor_real.login("idoso1", "senha-idoso", "app", "cel1")["access_token"]
    r = servidor_real.post("/dados", _dados("x" * (LIMITE_TESTE * 2)), t)
    assert r.status_code == 413
    assert servidor_real.post("/dados", _dados(), t).status_code == 200   # o servidor segue atendendo
