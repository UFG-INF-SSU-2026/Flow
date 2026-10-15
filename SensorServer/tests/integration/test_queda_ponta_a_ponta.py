"""
Queda de ponta a ponta com servidor real (equivale a simular_queda.py): cenarios (1), (2) e (3) do plano.

O tempo da regra de queda e da mensagem e congelado pela fixture `relogio` e avancado pelo teste, entao nao ha
espera real de 3 s de repouso nem de 7 s de prazo. A thread vigia e as conexoes SSE sao reais.
"""
import pytest

import const
import mensagens
import regra_queda
from _apoio import aguardar


class Cenario:
    """idoso1 (app) e bridge1 (wokwi) conectados nos dois canais, mais atalhos para os envios."""

    def __init__(self, srv, relogio):
        self.srv, self.relogio, self._seq = srv, relogio, 0
        self.tok_app = srv.login("idoso1", "senha-idoso", "app", "cel1")["access_token"]
        self.tok_wk = srv.login("bridge1", "senha-bridge", "wokwi", "bridge-dev")["access_token"]
        self.sse_app = srv.sse("/push/app", self.tok_app)
        self.sse_wk = srv.sse("/push/wokwi", self.tok_wk)
        for s in (self.sse_app, self.sse_wk):
            s.esperar_comentario("conectado")

    def fechar(self):
        self.sse_app.fechar()
        self.sse_wk.fechar()

    def _proxima_seq(self) -> int:
        self._seq += 1
        return self._seq

    def leitura_wokwi(self, value, vmax):
        seq = self._proxima_seq()
        ev = {"schemaVersion": 3, "eventType": "imobilidade.leitura", "deviceId": "bridge-dev",
              "entityId": "idoso-sim", "eventTimeMs": seq * 1000, "sequence": seq, "value": value,
              "valueMax": vmax, "unit": "m/s2", "state": "NORMAL", "user_id": 1, "source": "wokwi"}
        r = self.srv.post("/dados", ev, self.tok_wk)
        assert r.status_code == 200 and not r.json().get("descartado"), r.text

    def provocar_queda(self):
        """Leitura normal, impacto e T_POS_IMPACTO_S segundos de repouso (1 s de relogio por leitura).
        Devolve o pedido recebido em cada canal: (no app, no wokwi)."""
        self.leitura_wokwi(1.0, 1.5)
        self.relogio.avancar(1)
        self.leitura_wokwi(3.0, 25.0)
        for _ in range(int(const.T_POS_IMPACTO_S)):
            self.relogio.avancar(1)
            self.leitura_wokwi(0.3, 0.4)
        return self.sse_app.proximo_push(), self.sse_wk.proximo_push()

    def ack_bridge(self, message_id):
        seq = self._proxima_seq()
        r = self.srv.post("/eventos", {
            "schemaVersion": 3, "source": "wokwi", "user_id": 1, "deviceId": "bridge-dev", "entityId": "idoso-sim",
            "eventTimeMs": seq * 1000, "sequence": seq, "eventType": "push.ack", "state": "ATIVADO",
            "messageId": message_id}, self.tok_wk)
        assert r.status_code == 200 and r.json() == {"status": "ok"}, r.text

    def resposta_app(self, message_id, state="ESTOU_BEM"):
        r = self.srv.post("/eventos", {
            "schemaVersion": 3, "source": "app", "user_id": 1, "device_id": "cel1",
            "event_time": "2026-10-14T12:00:00.000Z", "eventType": "confirmacao.resposta", "state": state,
            "messageId": message_id}, self.tok_app)
        assert r.status_code == 200 and r.json() == {"status": "ok"}, r.text


@pytest.fixture
def cenario(relogio, servidor_real):
    c = Cenario(servidor_real, relogio)
    yield c
    c.fechar()


def test_1_pedido_chega_nos_dois_canais(cenario, ler_confirmacoes):
    msg_app, msg_wk = cenario.provocar_queda()

    assert msg_app["messageId"] == msg_wk["messageId"]
    assert msg_app["episodeId"] == msg_wk["episodeId"]
    for m in (msg_app, msg_wk):
        assert m["messageType"] == mensagens.TIPO_SOLICITADA
        assert m["userId"] == 1
        assert m["payload"]["timeoutMs"] == 5000        # tempo congelado: sem desconto de replay
        assert m["payload"]["motivo"] == const.MOTIVO_A

    entregas = [l for l in ler_confirmacoes(1) if l["tipo"] == "push.entrega"]
    assert {(l["canal"], l["resultado"]) for l in entregas} == {("app", "ENTREGUE_AO_CANAL"),
                                                                 ("wokwi", "ENTREGUE_AO_CANAL")}


def test_2_ack_e_estou_bem_encerram_como_confirmado(cenario, ler_confirmacoes):
    pedido, _ = cenario.provocar_queda()
    mid = pedido["messageId"]

    cenario.ack_bridge(mid)
    cenario.resposta_app(mid)

    enc_app, enc_wk = cenario.sse_app.proximo_push(), cenario.sse_wk.proximo_push()
    for enc in (enc_app, enc_wk):
        assert enc["messageType"] == mensagens.TIPO_ENCERRADA
        assert enc["payload"]["resultado"] == "CONFIRMADO"
        assert enc["episodeId"] == pedido["episodeId"]
        assert enc["messageId"] != mid
    assert regra_queda._episodios[1].resultado == "CONFIRMADO"
    assert [l["tipo"] for l in ler_confirmacoes(1)] == [
        "episodio.aberto", "push.entrega", "push.entrega", "push.ack", "confirmacao.resposta",
        "episodio.encerrado", "push.entrega", "push.entrega"]


def test_3_sem_resposta_expira_depois_de_timeout_mais_margem(cenario, ler_confirmacoes):
    pedido, _ = cenario.provocar_queda()

    cenario.relogio.avancar(const.TIMEOUT_CONFIRMACAO_S)        # passou o tempo do idoso, falta a margem
    assert cenario.sse_app.sem_push(0.4) and cenario.sse_wk.sem_push(0.4)
    assert regra_queda._episodios[1].encerrado_em is None

    cenario.relogio.avancar(const.MARGEM_SERVIDOR_S)            # agora vence o prazo do servidor
    enc_app, enc_wk = cenario.sse_app.proximo_push(), cenario.sse_wk.proximo_push()
    for enc in (enc_app, enc_wk):
        assert enc["messageType"] == mensagens.TIPO_ENCERRADA
        assert enc["payload"]["resultado"] == "EXPIRADO"
        assert enc["episodeId"] == pedido["episodeId"]
    assert regra_queda._episodios[1].resultado == "EXPIRADO"

    # a ultima linha de entrega e gravada logo depois do envio; espera ela aparecer
    assert aguardar(lambda: len(ler_confirmacoes(1)) >= 6)
    assert [l["tipo"] for l in ler_confirmacoes(1)] == [
        "episodio.aberto", "push.entrega", "push.entrega",
        "episodio.encerrado", "push.entrega", "push.entrega"]
