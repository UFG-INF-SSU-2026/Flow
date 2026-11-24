"""Rotas do cuidador: estado e historico so dos user_ids do token, cifrados, sem localizacao; banco fora da 503."""
from datetime import timedelta

import pytest

from borda import auth, tempo
from flow_db import repositorios


@pytest.fixture
def cuidador(cli, contas):
    auth.criar_conta("cuidador1", "cuidador", [1], ["app"], "senha-cuidador")
    return cli.login("cuidador1", "senha-cuidador", "app", "cel-cuidador")["access_token"]


@pytest.fixture
def historico_do_usuario_1(banco):
    agora = tempo.agora_utc()
    banco.gravar_leituras([{"instante_ingestao": agora - timedelta(seconds=5), "user_id": 1, "device_id": "cel1",
                            "fonte": "app", "tipo_evento": "telemetria", "schema_version": 3, "instante_origem": None,
                            "conta_id": "idoso1", "payload": {"localizacao": {"latitude": -16.6, "longitude": -49.2}}}])
    banco.gravar_alerta({"instante": agora, "user_id": 1, "device_id": "cel1", "tipo": "luz", "mensagem": "luz inadequada",
                         "dados": {}, "episodio_id": None, "schema_version": 3})
    banco.gravar_confirmacao({"instante": agora, "user_id": 1, "tipo": "confirmacao.resposta", "episodio_id": "e1",
                              "dados": {"state": "ESTOU_BEM", "deviceId": "cel1", "messageId": "m1"},
                              "message_id": "m1", "schema_version": 3})
    banco.gravar_episodio({"episodio_id": "e1", "user_id": 1, "device_id": None, "tipo": "QUEDA", "estado": "aguardando_confirmacao",
                           "aberto_em": agora, "atualizado_em": agora, "encerrado_em": None, "motivo_encerramento": None,
                           "dados": {}})


def test_estado_do_proprio_idoso(cli, cuidador, historico_do_usuario_1):
    r = cli.get("/cuidador/1/estado", cuidador)
    assert r.status_code == 200
    e = cli.corpo(r)
    assert e["userId"] == 1 and e["geradoEm"]
    assert [c["deviceId"] for c in e["comunicacoes"]] == ["cel1"]
    assert e["episodioAberto"]["episodioId"] == "e1"
    assert [a["mensagem"] for a in e["alertas"]] == ["luz inadequada"]
    assert "latitude" not in r.get_data(as_text=True) and "latitude" not in str(e)


def test_historico_do_proprio_idoso(cli, cuidador, historico_do_usuario_1):
    h = cli.corpo(cli.get("/cuidador/1/historico", cuidador))
    assert [c["estado"] for c in h["confirmacoes"]] == ["ESTOU_BEM"]
    assert set(h["confirmacoes"][0]) == {"instante", "tipo", "episodioId", "estado"}      # sem deviceId nem messageId
    assert len(h["alertas"]) == 1 and len(h["episodios"]) == 1 and "latitude" not in str(h)


def test_outra_pessoa_e_recusada(cli, cuidador):
    r = cli.get("/cuidador/2/historico", cuidador)
    assert r.status_code == 403 and r.get_json()["codigo"] == "fora_de_escopo"


def test_login_do_cuidador_exige_device_id(cli, contas):
    auth.criar_conta("cuidador1", "cuidador", [1], ["app"], "senha-cuidador")
    r = cli.post("/auth/login", {"schemaVersion": 3, "identificador": "cuidador1", "segredo": "senha-cuidador", "fonte": "app"})
    assert r.status_code == 400


def test_banco_fora_da_503(cli, cuidador, monkeypatch):
    def quebrado():
        raise ConnectionError("banco fora")
    monkeypatch.setattr(repositorios, "repo", quebrado)
    assert cli.get("/cuidador/1/estado", cuidador).status_code == 503
