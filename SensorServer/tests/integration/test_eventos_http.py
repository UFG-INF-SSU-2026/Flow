"""POST /eventos pelo test_client: push.ack e confirmacao.resposta, descarte de conteudo invalido e messageId desconhecida."""
import time

import pytest

import canal_app
import canal_wokwi
import const
import mensagens
import regra_queda
import server
from sse_hub import HUB

RETORNOS = [("push.ack", "ATIVADO"), ("push.ack", "REJEITADO"),
            ("confirmacao.resposta", "ESTOU_BEM"), ("confirmacao.resposta", "SEM_RESPOSTA")]


def _app(tipo="push.ack", state="ATIVADO", message_id="m-desconhecida", **extra) -> dict:
    corpo = {"schemaVersion": const.SCHEMA_VERSION_APP, "source": "app", "user_id": 1, "device_id": "cel1",
             "event_time": "2026-10-14T12:00:00.000Z", "eventType": tipo, "state": state, "messageId": message_id}
    corpo.update(extra)
    return corpo


def _wokwi(tipo="push.ack", state="ATIVADO", message_id="m-desconhecida", seq=1, **extra) -> dict:
    corpo = {"schemaVersion": const.SCHEMA_VERSION_WOKWI, "source": "wokwi", "user_id": 1, "deviceId": "bridge-dev", "entityId": "idoso-sim",
             "eventTimeMs": seq * 1000, "sequence": seq, "eventType": tipo, "state": state, "messageId": message_id}
    corpo.update(extra)
    return corpo


def _sem(corpo: dict, *campos) -> dict:
    return {k: v for k, v in corpo.items() if k not in campos}


def _abrir_episodio(user_id: int = 1):
    """Abre um episodio (gatilho B) e registra a mensagem do pedido, como server._emitir_pedido faz.
    Devolve (pedido, mensagem). Nao despacha para os canais."""
    pedido = regra_queda.abrir_por_imobilidade(user_id)
    msg = mensagens.criar_pedido_confirmacao(user_id, pedido.episode_id, pedido.timeout_s, pedido.motivo)
    regra_queda.anotar_mensagem(user_id, pedido.episode_id, msg["messageId"])
    return pedido, msg


def _sem_abertura(ler_confirmacoes, user_id: int = 1) -> list[dict]:
    """Linhas do usuario sem a `episodio.aberto` que _abrir_episodio grava."""
    return [l for l in ler_confirmacoes(user_id) if l["tipo"] != "episodio.aberto"]


def _encerramentos_no_anel(user_id: int = 1) -> dict[str, list[dict]]:
    """Mensagens queda.confirmacao_encerrada que cada canal guardou, por canal."""
    return {canal: [m for _, m in HUB._anel.get((user_id, canal), []) if m["messageType"] == mensagens.TIPO_ENCERRADA]
            for canal in (canal_app.CANAL, canal_wokwi.CANAL)}


# ------------------------------- eventos validos -----------------------------
class TestEventosValidos:
    @pytest.mark.parametrize("tipo,state", RETORNOS)
    def test_app_da_200_e_grava_a_linha(self, cli, tokens, ler_confirmacoes, tipo, state):
        r = cli.post("/eventos", _app(tipo, state, "m-1"), tokens["idoso"])
        assert r.status_code == 200 and r.get_json() == {"status": "ok"}
        (linha,) = ler_confirmacoes(1)
        assert linha["tipo"] == tipo and linha["timestamp"]
        assert (linha["eventType"], linha["state"], linha["messageId"]) == (tipo, state, "m-1")
        assert (linha["source"], linha["device_id"]) == ("app", "cel1")

    @pytest.mark.parametrize("tipo,state", RETORNOS)
    def test_wokwi_da_200_e_grava_a_linha(self, cli, tokens, ler_confirmacoes, tipo, state):
        r = cli.post("/eventos", _wokwi(tipo, state, "m-2"), tokens["dispositivo"])
        assert r.status_code == 200 and r.get_json() == {"status": "ok"}
        (linha,) = ler_confirmacoes(1)
        assert linha["tipo"] == tipo and linha["messageId"] == "m-2"
        assert (linha["source"], linha["device_id"]) == ("wokwi", "bridge-dev")

    def test_app_aceita_deviceId_no_lugar_de_device_id(self, cli, tokens, ler_confirmacoes):
        corpo = {**_sem(_app(), "device_id"), "deviceId": "cel1"}
        assert cli.post("/eventos", corpo, tokens["idoso"]).get_json() == {"status": "ok"}
        assert ler_confirmacoes(1)[0]["device_id"] == "cel1"

    def test_schemaversion_ausente_ou_menor_e_aceito(self, cli, tokens, ler_confirmacoes):
        for versao in (None, const.SCHEMA_VERSION_MINIMA, const.SCHEMA_VERSION_APP - 1):
            corpo = _sem(_app(), "schemaVersion") if versao is None else _app(schemaVersion=versao)
            r = cli.post("/eventos", corpo, tokens["idoso"])
            assert r.status_code == 200 and r.get_json() == {"status": "ok"}
        assert len(ler_confirmacoes(1)) == 3

    def test_nao_passa_pelo_buffer_de_agregacao(self, cli, tokens):
        cli.post("/eventos", _app(), tokens["idoso"])
        assert server._buffer == {}


# ------------------------------ evento invalido ------------------------------
class TestEventoInvalidoEDescartado:
    @pytest.mark.parametrize("corpo", [
        pytest.param(_app(tipo="outro.evento"), id="eventType_desconhecido"),
        pytest.param(_app(tipo=None), id="eventType_nulo"),
        pytest.param(_app("push.ack", "ESTOU_BEM"), id="state_de_outro_eventType_1"),
        pytest.param(_app("confirmacao.resposta", "ATIVADO"), id="state_de_outro_eventType_2"),
        pytest.param(_app(state="INEXISTENTE"), id="state_inexistente"),
        pytest.param(_app(message_id=""), id="messageId_vazio"),
        pytest.param(_app(message_id=123), id="messageId_nao_string"),
        pytest.param(_sem(_app(), "messageId"), id="messageId_ausente"),
        pytest.param(_sem(_app(), "device_id"), id="sem_device_id_nem_deviceId"),
        pytest.param(_app(device_id=""), id="device_id_vazio"),
        pytest.param(_sem(_app(), "event_time"), id="app_sem_event_time"),
    ])
    def test_app_invalido_da_200_descartado_e_nao_grava(self, cli, tokens, ler_confirmacoes, corpo):
        r = cli.post("/eventos", corpo, tokens["idoso"])
        assert r.status_code == 200 and r.get_json() == {"status": "ok", "descartado": True}
        assert ler_confirmacoes(1) == []

    @pytest.mark.parametrize("faltando", ["entityId", "eventTimeMs", "sequence"])
    def test_wokwi_sem_campo_obrigatorio_e_descartado(self, cli, tokens, ler_confirmacoes, faltando):
        r = cli.post("/eventos", _sem(_wokwi(), faltando), tokens["dispositivo"])
        assert r.status_code == 200 and r.get_json()["descartado"] is True
        assert ler_confirmacoes(1) == []

    def test_invalido_nao_encerra_episodio_aberto(self, cli, tokens):
        pedido, msg = _abrir_episodio()
        r = cli.post("/eventos", _app("confirmacao.resposta", "TUDO_BEM", msg["messageId"]), tokens["idoso"])
        assert r.get_json()["descartado"] is True
        assert regra_queda._episodios[1].encerrado_em is None


# --------------------------- schemaVersion nao suportado ----------------------
class TestSchemaVersionNaoSuportado:
    @pytest.mark.parametrize("versao", [const.SCHEMA_VERSION_APP + 1, 99, 0, -1, "3", 3.0, True, None, [3]], ids=repr)
    def test_da_200_descartado_e_nao_grava(self, cli, tokens, ler_confirmacoes, versao):
        r = cli.post("/eventos", _app(schemaVersion=versao), tokens["idoso"])
        assert r.status_code == 200
        assert r.get_json() == {"status": "ok", "descartado": True, "motivo": "schemaVersion_nao_suportado"}
        assert ler_confirmacoes(1) == []

    def test_versao_nao_suportada_nao_encerra_episodio_aberto(self, cli, tokens):
        pedido, msg = _abrir_episodio()
        corpo = _app("confirmacao.resposta", "ESTOU_BEM", msg["messageId"], schemaVersion=const.SCHEMA_VERSION_APP + 1)
        assert cli.post("/eventos", corpo, tokens["idoso"]).get_json()["descartado"] is True
        assert regra_queda._episodios[1].encerrado_em is None

    def test_versao_vale_tambem_para_o_wokwi(self, cli, tokens, ler_confirmacoes):
        r = cli.post("/eventos", _wokwi(schemaVersion=const.SCHEMA_VERSION_WOKWI + 1), tokens["dispositivo"])
        assert r.get_json()["descartado"] is True and ler_confirmacoes(1) == []


# ------------------------------ messageId desconhecida -------------------------
class TestMensagemDesconhecida:
    @pytest.mark.parametrize("tipo,state", RETORNOS)
    def test_e_gravada_com_episodio_nulo_e_a_marca(self, cli, tokens, ler_confirmacoes, tipo, state):
        r = cli.post("/eventos", _app(tipo, state, "nunca-emitida"), tokens["idoso"])
        assert r.status_code == 200 and r.get_json() == {"status": "ok"}
        (linha,) = ler_confirmacoes(1)
        assert linha["tipo"] == tipo and linha["messageId"] == "nunca-emitida"
        assert linha["episodeId"] is None and linha["mensagem_desconhecida"] is True
        assert "tardia" not in linha

    def test_estou_bem_desconhecido_nao_encerra_nem_avisa_ninguem(self, cli, tokens):
        pedido, msg = _abrir_episodio()
        cli.post("/eventos", _app("confirmacao.resposta", "ESTOU_BEM", "outra-mensagem"), tokens["idoso"])
        assert regra_queda._episodios[1].encerrado_em is None
        assert _encerramentos_no_anel() == {"app": [], "wokwi": []}

    def test_mensagem_de_outro_usuario_conta_como_desconhecida(self, cli, tokens, ler_confirmacoes):
        pedido, msg = _abrir_episodio(user_id=2)               # a mensagem e do usuario 2; o token e do usuario 1
        r = cli.post("/eventos", _app("confirmacao.resposta", "ESTOU_BEM", msg["messageId"]), tokens["idoso"])
        assert r.status_code == 200
        (linha,) = ler_confirmacoes(1)
        assert linha["mensagem_desconhecida"] is True and linha["episodeId"] is None
        assert regra_queda._episodios[2].encerrado_em is None


# ------------------------------- mensagem conhecida ---------------------------
class TestMensagemConhecida:
    def test_estou_bem_encerra_como_confirmado_e_avisa_os_dois_canais(self, cli, tokens, ler_confirmacoes):
        pedido, msg = _abrir_episodio()
        r = cli.post("/eventos", _app("confirmacao.resposta", "ESTOU_BEM", msg["messageId"]), tokens["idoso"])
        assert r.status_code == 200 and r.get_json() == {"status": "ok"}

        assert regra_queda._episodios[1].resultado == "CONFIRMADO"
        assert [l["tipo"] for l in _sem_abertura(ler_confirmacoes)] == [
            "confirmacao.resposta", "episodio.encerrado", "push.entrega", "push.entrega"]
        resposta, encerrado, *entregas = _sem_abertura(ler_confirmacoes)
        assert resposta["episodeId"] == pedido.episode_id and "mensagem_desconhecida" not in resposta
        assert encerrado["resultado"] == "CONFIRMADO"
        assert {(l["canal"], l["resultado"]) for l in entregas} == {("app", "SEM_CANAL_ATIVO"),
                                                                    ("wokwi", "SEM_CANAL_ATIVO")}
        for canal, guardadas in _encerramentos_no_anel().items():   # sem conexao, mas o anel guarda para o replay
            assert [m["payload"]["resultado"] for m in guardadas] == ["CONFIRMADO"], canal
            assert guardadas[0]["episodeId"] == pedido.episode_id

    @pytest.mark.parametrize("tipo,state", [("push.ack", "ATIVADO"), ("push.ack", "REJEITADO"),
                                            ("confirmacao.resposta", "SEM_RESPOSTA")])
    def test_ack_e_sem_resposta_so_gravam(self, cli, tokens, ler_confirmacoes, tipo, state):
        pedido, msg = _abrir_episodio()
        cli.post("/eventos", _app(tipo, state, msg["messageId"]), tokens["idoso"])

        assert regra_queda._episodios[1].encerrado_em is None
        (linha,) = _sem_abertura(ler_confirmacoes)
        assert linha["tipo"] == tipo and linha["episodeId"] == pedido.episode_id
        assert _encerramentos_no_anel() == {"app": [], "wokwi": []}

    def test_retorno_depois_do_encerramento_e_tardio(self, cli, tokens, ler_confirmacoes):
        pedido, msg = _abrir_episodio()
        regra_queda.vigiar(time.time() + 1000)                  # prazo vencido: EXPIRADO
        assert regra_queda._episodios[1].resultado == "EXPIRADO"
        antes = len(ler_confirmacoes(1))

        r = cli.post("/eventos", _app("confirmacao.resposta", "ESTOU_BEM", msg["messageId"]), tokens["idoso"])
        assert r.status_code == 200 and r.get_json() == {"status": "ok"}
        (linha,) = ler_confirmacoes(1)[antes:]
        assert linha["tardia"] is True and linha["episodeId"] == pedido.episode_id
        assert regra_queda._episodios[1].resultado == "EXPIRADO"


# --------------------------- autorizacao (conferencia rapida) -----------------
def test_user_id_de_outra_pessoa_da_403_e_nao_grava(cli, tokens, ler_confirmacoes):
    r = cli.post("/eventos", _app(user_id=2), tokens["idoso"])
    assert r.status_code == 403
    assert ler_confirmacoes(1) == [] and ler_confirmacoes(2) == []
