"""
Fluxo de queda compartilhado pelos testes com servidor real (ponta a ponta e resiliencia do push).

`FluxoQueda` faz o papel do app (idoso1, dispositivo `cel1`) e do bridge (bridge1, dispositivo `bridge-dev`):
login, telemetria, eventos de retorno e atalhos para provocar a queda. NAO abre stream SSE; quem precisa dele
(ver Cenario em test_queda_ponta_a_ponta.py) abre por cima.

O tempo da regra de queda e da mensagem vem da fixture `relogio` (congelado, avancado pelo teste). Cada leitura
simulada vale 1 s de relogio.
"""
import const


class FluxoQueda:
    def __init__(self, srv, relogio):
        """`srv`: ClienteReal (servidor numa porta efemera). `relogio`: fixture `relogio`."""
        self.srv, self.relogio, self._seq, self._ts_app = srv, relogio, 0, 1_700_000_000_000
        self.tok_app = srv.login("idoso1", "senha-idoso", "app", "cel1")["access_token"]
        self.tok_wk = srv.login("bridge1", "senha-bridge", "wokwi", "bridge-dev")["access_token"]

    def _proxima_seq(self) -> int:
        self._seq += 1
        return self._seq

    # ------------------------------ telemetria ------------------------------
    def _dados_wokwi(self, evento: dict) -> None:
        r = self.srv.post("/dados", evento, self.tok_wk)
        assert r.status_code == 200 and not r.json().get("descartado"), r.text

    def leitura_wokwi(self, value, vmax) -> None:
        seq = self._proxima_seq()
        self._dados_wokwi({
            "schemaVersion": 3, "eventType": "imobilidade.leitura", "deviceId": "bridge-dev",
            "entityId": "idoso-sim", "eventTimeMs": seq * 1000, "sequence": seq, "value": value,
            "valueMax": vmax, "unit": "m/s2", "state": "NORMAL", "user_id": 1, "source": "wokwi"})

    def decisao_wokwi(self, state: str, value: float = 0.3) -> None:
        """Evento imobilidade.decisao (por exemplo ALERTA_IMOBILIDADE, que alimenta o gatilho B)."""
        seq = self._proxima_seq()
        self._dados_wokwi({
            "schemaVersion": 3, "eventType": "imobilidade.decisao", "deviceId": "bridge-dev",
            "entityId": "idoso-sim", "eventTimeMs": seq * 1000, "sequence": seq, "value": value,
            "unit": "m/s2", "state": state, "user_id": 1, "source": "wokwi"})

    def leitura_app(self, magnitude, magnitude_max) -> None:
        """Telemetria do app com `acelerometro`. `timestamp_envio` cresce a cada chamada (senao seria duplicata)."""
        self._ts_app += 1000
        r = self.srv.post("/dados", {
            "schemaVersion": 3, "source": "app", "user_id": 1, "device_id": "cel1",
            "timestamp_envio": self._ts_app,
            "acelerometro": {"magnitude": magnitude, "magnitude_max_janela": magnitude_max}}, self.tok_app)
        assert r.status_code == 200 and not r.json().get("descartado"), r.text

    def enviar_queda(self) -> None:
        """Leitura normal, impacto e T_POS_IMPACTO_S segundos de repouso (wokwi). Nao le nenhum push."""
        self.leitura_wokwi(1.0, 1.5)
        self.relogio.avancar(1)
        self.leitura_wokwi(3.0, 25.0)
        for _ in range(int(const.T_POS_IMPACTO_S)):
            self.relogio.avancar(1)
            self.leitura_wokwi(0.3, 0.4)

    # ------------------------- eventos de retorno ---------------------------
    def ack_bridge(self, message_id: str) -> None:
        seq = self._proxima_seq()
        r = self.srv.post("/eventos", {
            "schemaVersion": 3, "source": "wokwi", "user_id": 1, "deviceId": "bridge-dev", "entityId": "idoso-sim",
            "eventTimeMs": seq * 1000, "sequence": seq, "eventType": "push.ack", "state": "ATIVADO",
            "messageId": message_id}, self.tok_wk)
        assert r.status_code == 200 and r.json() == {"status": "ok"}, r.text

    def resposta_app(self, message_id: str, state: str = "ESTOU_BEM") -> None:
        r = self.srv.post("/eventos", {
            "schemaVersion": 3, "source": "app", "user_id": 1, "device_id": "cel1",
            "event_time": "2026-10-14T12:00:00.000Z", "eventType": "confirmacao.resposta", "state": state,
            "messageId": message_id}, self.tok_app)
        assert r.status_code == 200 and r.json() == {"status": "ok"}, r.text
