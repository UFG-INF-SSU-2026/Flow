"""Falha de canal e do aviso de encerramento nao derrubam o fluxo."""
import const
import canal_app
import canal_wokwi
import mensagens
import regra_queda
import server
from sse_hub import HUB


class Fluxo:
    """Envia telemetria do wokwi pelo test_client e provoca a queda (1 s de relogio por leitura)."""

    def __init__(self, cli, tokens, relogio):
        self.cli, self.tokens, self.relogio, self._seq = cli, tokens, relogio, 0

    def leitura(self, value, vmax):
        self._seq += 1
        ev = {"schemaVersion": const.SCHEMA_VERSION_WOKWI, "eventType": "imobilidade.leitura", "deviceId": "bridge-dev",
              "entityId": "idoso-sim", "eventTimeMs": self._seq * 1000, "sequence": self._seq, "value": value,
              "valueMax": vmax, "unit": "m/s2", "state": "NORMAL", "user_id": 1, "source": "wokwi"}
        return self.cli.post("/dados", ev, self.tokens["dispositivo"])

    def provocar_queda(self):
        """Devolve a resposta HTTP da leitura que fecha a condicao (a que dispara o pedido)."""
        self.leitura(1.0, 1.5)
        self.relogio.avancar(1)
        self.leitura(3.0, 25.0)
        r = None
        for _ in range(int(const.T_POS_IMPACTO_S)):
            self.relogio.avancar(1)
            r = self.leitura(0.3, 0.4)
        return r


def _entregas(ler_confirmacoes):
    return [(l["canal"], l["resultado"]) for l in ler_confirmacoes(1) if l["tipo"] == "push.entrega"]


def _resposta_app(cli, tokens, message_id):
    return cli.post("/eventos", {
        "schemaVersion": const.SCHEMA_VERSION_APP, "source": "app", "user_id": 1, "device_id": "cel1",
        "event_time": "2026-10-14T12:00:00.000Z", "eventType": "confirmacao.resposta", "state": "ESTOU_BEM",
        "messageId": message_id}, tokens["idoso"])


def test_canal_app_falha_e_o_wokwi_ainda_recebe_o_pedido(cli, tokens, relogio, monkeypatch, ler_confirmacoes):
    def quebrado(user_id, msg):
        raise RuntimeError("canal app quebrado")

    monkeypatch.setattr(canal_app, "enviar", quebrado)
    r = Fluxo(cli, tokens, relogio).provocar_queda()

    assert r.status_code == 200                                      # nao virou 500
    assert _entregas(ler_confirmacoes) == [("app", "ERRO_NO_CANAL"), ("wokwi", "SEM_CANAL_ATIVO")]
    (guardada,) = [m for _, m in HUB._anel[(1, canal_wokwi.CANAL)]]    # o wokwi publicou (ficou no anel)
    assert guardada["messageType"] == mensagens.TIPO_SOLICITADA
    assert (1, canal_app.CANAL) not in HUB._anel
    assert regra_queda._episodios[1].encerrado_em is None             # o episodio segue aberto


def test_falha_ao_emitir_o_pedido_nao_da_500_em_dados(cli, tokens, relogio, monkeypatch):
    def quebrado(*a, **k):
        raise RuntimeError("emitir quebrou")

    monkeypatch.setattr(server, "_emitir_pedido", quebrado)
    assert Fluxo(cli, tokens, relogio).provocar_queda().status_code == 200


def test_estou_bem_da_200_mesmo_se_o_aviso_de_encerramento_falhar(cli, tokens, relogio, monkeypatch):
    Fluxo(cli, tokens, relogio).provocar_queda()
    (message_id,) = regra_queda._mensagens

    def quebrado(*a, **k):
        raise RuntimeError("despacho quebrou")

    monkeypatch.setattr(server, "_despachar", quebrado)
    r = _resposta_app(cli, tokens, message_id)
    assert r.status_code == 200 and r.get_json() == {"status": "ok"}
    assert regra_queda._episodios[1].resultado == "CONFIRMADO"


def test_canal_que_falha_no_encerramento_nao_impede_o_outro(cli, tokens, relogio, monkeypatch, ler_confirmacoes):
    Fluxo(cli, tokens, relogio).provocar_queda()
    (message_id,) = regra_queda._mensagens

    def quebrado(user_id, msg):
        raise RuntimeError("canal app quebrado")

    monkeypatch.setattr(canal_app, "enviar", quebrado)
    assert _resposta_app(cli, tokens, message_id).status_code == 200
    ultimos = [(l["canal"], l["resultado"]) for l in ler_confirmacoes(1)[-2:]]
    assert ultimos == [("app", "ERRO_NO_CANAL"), ("wokwi", "SEM_CANAL_ATIVO")]
    encerradas = [m for _, m in HUB._anel[(1, canal_wokwi.CANAL)]
                  if m["messageType"] == mensagens.TIPO_ENCERRADA]
    assert len(encerradas) == 1 and encerradas[0]["payload"]["resultado"] == "CONFIRMADO"
