"""API local do bridge: autenticacao pelo token de sessao local, roteamento, carimbo e isolamento entre dispositivos."""
import pytest

import api as api_mod
import sessao as sessao_mod
from apoio import USER_ID, pedido
from sessao import ErroServidor, Relogin, Sessao
from test_sessao import RespostaFalsa, servidor_falso, tokens  # noqa: F401  (fixture e apoio)

LOGIN = {"identificador": "conta-teste", "segredo": "segredo-teste", "deviceId": "dev-a"}


@pytest.fixture(autouse=True)
def sem_thread_de_push(monkeypatch):
    monkeypatch.setattr(Sessao, "iniciar_push", lambda self: None)


@pytest.fixture
def cliente(cfg):
    return api_mod.criar_app(cfg).test_client()


def logar(cliente, servidor_falso, **mudancas):
    servidor_falso.respostas.append(tokens("A", "R"))
    r = cliente.post("/v1/login", json={**LOGIN, **mudancas})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["token"], r.get_json()["deviceId"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ login e token local
def test_evento_sem_token_de_sessao_local_e_recusado(cliente, servidor_falso):
    logar(cliente, servidor_falso)
    r = cliente.post("/v1/dispositivos/dev-a/eventos", json={"eventType": "push.ack"})
    assert r.status_code == 401
    assert [c[0] for c in servidor_falso.chamadas] == ["/auth/login"]       # nada foi ao servidor


def test_login_e_repassado_ao_servidor_sem_o_bridge_guardar_o_segredo(cliente, servidor_falso, cfg):
    token, _ = logar(cliente, servidor_falso)
    (rota, corpo, _), = servidor_falso.chamadas
    assert rota == "/auth/login"
    assert (corpo["identificador"], corpo["segredo"], corpo["device_id"]) == ("conta-teste", "segredo-teste", "dev-a")
    reg = cliente.application.extensions["dispositivos"]
    assert "segredo-teste" not in repr(vars(reg.sessao("dev-a"))) + repr(vars(reg))
    assert token != "A"                           # o token local nao e o do servidor


def test_login_recusado_pelo_servidor_volta_com_o_mesmo_codigo(cliente, servidor_falso):
    servidor_falso.respostas.append(RespostaFalsa(401))
    assert cliente.post("/v1/login", json=LOGIN).status_code == 401
    servidor_falso.respostas.append(RespostaFalsa(503))
    assert cliente.post("/v1/login", json=LOGIN).status_code == 502


@pytest.mark.parametrize("corpo", [None, [], {}, {**LOGIN, "deviceId": "a/b"}, {**LOGIN, "segredo": ""}, {**LOGIN, "deviceId": 5}])
def test_login_malformado_nao_chega_ao_servidor(cliente, servidor_falso, corpo):
    assert cliente.post("/v1/login", json=corpo).status_code == 400
    assert servidor_falso.chamadas == []


def test_limite_de_dispositivos(cliente, servidor_falso):
    logar(cliente, servidor_falso, deviceId="d1")
    logar(cliente, servidor_falso, deviceId="d2")
    assert cliente.post("/v1/login", json={**LOGIN, "deviceId": "d3"}).status_code == 429
    logar(cliente, servidor_falso, deviceId="d1")                           # o mesmo dispositivo pode logar de novo


def test_novo_login_do_mesmo_dispositivo_invalida_o_token_antigo(cliente, servidor_falso):
    antigo, _ = logar(cliente, servidor_falso)
    novo, _ = logar(cliente, servidor_falso)
    assert cliente.get("/v1/dispositivos/dev-a/estado", headers=auth(antigo)).status_code == 401
    assert cliente.get("/v1/dispositivos/dev-a/estado", headers=auth(novo)).status_code == 200


# ------------------------------------------------------------------ eventos: roteamento e carimbo
@pytest.mark.parametrize("tipo, rota", [("push.ack", "/eventos"), ("confirmacao.resposta", "/eventos"),
                                        ("imobilidade.leitura", "/dados"), ("sistema.erro", "/dados")])
def test_roteamento_por_event_type(cliente, servidor_falso, tipo, rota):
    token, _ = logar(cliente, servidor_falso)
    servidor_falso.respostas.append(RespostaFalsa(200))
    assert cliente.post("/v1/dispositivos/dev-a/eventos", json={"eventType": tipo}, headers=auth(token)).status_code == 200
    assert servidor_falso.chamadas[-1][0] == rota


def test_bridge_carimba_user_id_e_source_e_sobrescreve_os_do_dispositivo(cliente, servidor_falso):
    token, _ = logar(cliente, servidor_falso)
    servidor_falso.respostas.append(RespostaFalsa(200))
    ev = {"eventType": "imobilidade.leitura", "state": "NORMAL", "user_id": 99, "source": "app", "deviceId": "dev-a"}
    cliente.post("/v1/dispositivos/dev-a/eventos", json=ev, headers=auth(token))
    enviado = servidor_falso.chamadas[-1][1]
    assert enviado["user_id"] == USER_ID and enviado["source"] == "wokwi"
    assert enviado["state"] == "NORMAL" and enviado["deviceId"] == "dev-a"   # o resto, intacto; o deviceId e do dispositivo


@pytest.mark.parametrize("corpo", [None, [1, 2], "texto", {}, {"eventType": 5}])
def test_evento_que_nao_e_objeto_com_event_type_leva_400(cliente, servidor_falso, corpo):
    token, _ = logar(cliente, servidor_falso)
    r = cliente.post("/v1/dispositivos/dev-a/eventos", json=corpo, headers=auth(token))
    assert r.status_code == 400


def test_erros_do_servidor_voltam_ao_dispositivo(cliente, servidor_falso):
    token, _ = logar(cliente, servidor_falso)
    for status_servidor, esperado in ((403, 403), (400, 400), (500, 502)):
        servidor_falso.respostas.append(RespostaFalsa(status_servidor))
        r = cliente.post("/v1/dispositivos/dev-a/eventos", json={"eventType": "push.ack"}, headers=auth(token))
        assert r.status_code == esperado


def test_refresh_recusado_responde_401_relogin(cliente, servidor_falso):
    token, _ = logar(cliente, servidor_falso)
    servidor_falso.respostas += [RespostaFalsa(401), RespostaFalsa(401)]      # evento recusado; refresh recusado
    r = cliente.post("/v1/dispositivos/dev-a/eventos", json={"eventType": "push.ack"}, headers=auth(token))
    assert r.status_code == 401 and r.get_json()["codigo"] == "relogin"


# ------------------------------------------------------------------ comandos, estado e encerramento
def test_comandos_entrega_e_esvazia_a_fila(cliente, servidor_falso):
    token, _ = logar(cliente, servidor_falso)
    sessao = cliente.application.extensions["dispositivos"].sessao("dev-a")
    msg = pedido()
    sessao._entregar(msg)
    r = cliente.get("/v1/dispositivos/dev-a/comandos", headers=auth(token))
    assert r.get_json() == {"comandos": [f"CMD;BUZZER_ON;{msg['messageId']};5000"]}
    assert cliente.get("/v1/dispositivos/dev-a/comandos", headers=auth(token)).get_json() == {"comandos": []}


def test_estado_nao_traz_token_nem_segredo(cliente, servidor_falso):
    token, _ = logar(cliente, servidor_falso)
    texto = cliente.get("/v1/dispositivos/dev-a/estado", headers=auth(token)).get_data(as_text=True)
    assert "segredo-teste" not in texto and token not in texto and '"A"' not in texto


def test_delete_encerra_a_sessao(cliente, servidor_falso):
    token, _ = logar(cliente, servidor_falso)
    assert cliente.delete("/v1/dispositivos/dev-a", headers=auth(token)).status_code == 204
    assert cliente.get("/v1/dispositivos/dev-a/estado", headers=auth(token)).status_code == 401


# ------------------------------------------------------------------ isolamento entre dispositivos
def test_dois_dispositivos_ficam_isolados(cliente, servidor_falso):
    ta, _ = logar(cliente, servidor_falso, deviceId="dev-a")
    tb, _ = logar(cliente, servidor_falso, deviceId="dev-b")
    reg = cliente.application.extensions["dispositivos"]

    # um recebe o comando de push; o outro nao
    reg.sessao("dev-a")._entregar(pedido())
    assert len(cliente.get("/v1/dispositivos/dev-a/comandos", headers=auth(ta)).get_json()["comandos"]) == 1
    assert cliente.get("/v1/dispositivos/dev-b/comandos", headers=auth(tb)).get_json() == {"comandos": []}

    # o token local de um nao abre as rotas do outro
    for caminho in ("comandos", "estado"):
        assert cliente.get(f"/v1/dispositivos/dev-b/{caminho}", headers=auth(ta)).status_code == 403
    assert cliente.post("/v1/dispositivos/dev-b/eventos", json={"eventType": "push.ack"}, headers=auth(ta)).status_code == 403
    assert cliente.delete("/v1/dispositivos/dev-b", headers=auth(ta)).status_code == 403

    # um evento com o deviceId do outro e recusado pelo servidor (que compara com o do token) e o bridge so repassa o 403
    servidor_falso.respostas.append(RespostaFalsa(403))
    r = cliente.post("/v1/dispositivos/dev-a/eventos", json={"eventType": "push.ack", "deviceId": "dev-b"}, headers=auth(ta))
    assert r.status_code == 403
    assert servidor_falso.chamadas[-1][1]["deviceId"] == "dev-b"       # o bridge nao o reescreveu
