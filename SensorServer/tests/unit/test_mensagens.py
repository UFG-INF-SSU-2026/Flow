"""mensagens.py: pedido e encerramento (prazos), envio com replay e validacao dos eventos de retorno."""
from datetime import datetime, timedelta, timezone

import pytest

import const
import mensagens

T0 = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)


def pedido(timeout_s=5, agora=T0):
    return mensagens.criar_pedido_confirmacao(1, "ep1", timeout_s, "IMPACTO_SEGUIDO_DE_IMOBILIDADE", agora=agora)


# ----------------------------- criacao ---------------------------------------
class TestCriarPedido:
    def test_campos_do_contrato(self):
        m = pedido()
        assert m["schemaVersion"] == const.SCHEMA_VERSION_ATUAL
        assert m["messageType"] == mensagens.TIPO_SOLICITADA
        assert m["userId"] == 1 and m["episodeId"] == "ep1" and m["messageId"]
        assert m["payload"]["motivo"] == "IMPACTO_SEGUIDO_DE_IMOBILIDADE"

    def test_expires_at_e_emissao_mais_timeout_mais_margem(self):
        m = pedido(timeout_s=5)
        assert mensagens.parse_iso_z(m["issuedAt"]) == T0
        esperado = T0 + timedelta(seconds=5 + const.MARGEM_SERVIDOR_S)
        assert mensagens.parse_iso_z(m["expiresAt"]) == esperado

    def test_timeout_ms_em_milissegundos(self):
        assert pedido(timeout_s=5)["payload"]["timeoutMs"] == 5000
        assert pedido(timeout_s=2.5)["payload"]["timeoutMs"] == 2500

    def test_message_id_unico(self):
        assert pedido()["messageId"] != pedido()["messageId"]

    def test_margem_vem_de_const_na_hora_da_chamada(self, monkeypatch):
        monkeypatch.setattr(const, "MARGEM_SERVIDOR_S", 10)
        assert mensagens.parse_iso_z(pedido()["expiresAt"]) == T0 + timedelta(seconds=15)


class TestCriarEncerramento:
    @pytest.mark.parametrize("resultado", ["CONFIRMADO", "EXPIRADO", "CANCELADO"])
    def test_resultados_validos(self, resultado):
        m = mensagens.criar_encerramento(1, "ep1", resultado, agora=T0)
        assert m["messageType"] == mensagens.TIPO_ENCERRADA
        assert m["payload"] == {"resultado": resultado}

    def test_ttl_de_10_segundos(self):
        m = mensagens.criar_encerramento(1, "ep1", "EXPIRADO", agora=T0)
        assert mensagens.parse_iso_z(m["expiresAt"]) == T0 + timedelta(seconds=const.TTL_ENCERRAMENTO_S)
        assert const.TTL_ENCERRAMENTO_S == 10

    @pytest.mark.parametrize("resultado", ["", "confirmado", "OUTRO", None])
    def test_resultado_invalido_levanta_value_error(self, resultado):
        with pytest.raises(ValueError):
            mensagens.criar_encerramento(1, "ep1", resultado)


class TestFormatoDeTempo:
    def test_iso_z_tem_milissegundos_e_z(self):
        assert mensagens.iso_z(T0 + timedelta(milliseconds=123)) == "2026-10-15T12:00:00.123Z"

    def test_ida_e_volta(self):
        assert mensagens.parse_iso_z(mensagens.iso_z(T0)) == T0


# ----------------------------- para_envio ------------------------------------
class TestParaEnvio:
    def test_expirado_devolve_none(self):
        m = pedido()
        depois = T0 + timedelta(seconds=5 + const.MARGEM_SERVIDOR_S)    # exatamente em expiresAt
        assert mensagens.para_envio(m, agora=depois) is None

    def test_encerramento_expirado_devolve_none(self):
        m = mensagens.criar_encerramento(1, "ep1", "EXPIRADO", agora=T0)
        assert mensagens.para_envio(m, agora=T0 + timedelta(seconds=const.TTL_ENCERRAMENTO_S + 1)) is None

    def test_encerramento_valido_vai_sem_alterar_o_payload(self):
        m = mensagens.criar_encerramento(1, "ep1", "CONFIRMADO", agora=T0)
        assert mensagens.para_envio(m, agora=T0 + timedelta(seconds=3))["payload"] == {"resultado": "CONFIRMADO"}

    def test_pedido_no_instante_da_emissao_mantem_o_timeout(self):
        assert mensagens.para_envio(pedido(), agora=T0)["payload"]["timeoutMs"] == 5000

    def test_replay_recalcula_o_timeout_como_o_que_resta(self):
        copia = mensagens.para_envio(pedido(), agora=T0 + timedelta(seconds=2))
        assert copia["payload"]["timeoutMs"] == 3000

    def test_nao_altera_a_mensagem_original(self):
        m = pedido()
        copia = mensagens.para_envio(m, agora=T0 + timedelta(seconds=2))
        assert copia is not m and copia["payload"] is not m["payload"]
        assert m["payload"]["timeoutMs"] == 5000

    def test_pedido_dentro_da_margem_nao_e_enviado(self):
        """Tempo do cliente esgotado (timeoutMs), mas expiresAt (com a margem do servidor) ainda nao."""
        m = pedido(timeout_s=5)
        dentro_da_margem = T0 + timedelta(seconds=5 + const.MARGEM_SERVIDOR_S - 0.5)
        assert mensagens.parse_iso_z(m["expiresAt"]) > dentro_da_margem
        assert mensagens.para_envio(m, agora=dentro_da_margem) is None

    def test_timeout_nunca_passa_do_original(self):
        copia = mensagens.para_envio(pedido(), agora=T0 - timedelta(seconds=1))   # relogio um pouco atras
        assert copia["payload"]["timeoutMs"] == 5000


# ------------------------ validar_evento_retorno -----------------------------
def ev_app(**extra):
    ev = {"eventType": "confirmacao.resposta", "state": "ESTOU_BEM", "messageId": "m1",
          "device_id": "cel1", "event_time": "2026-10-15T12:00:00.000Z"}
    ev.update(extra)
    return ev


def ev_wokwi(**extra):
    ev = {"eventType": "push.ack", "state": "ATIVADO", "messageId": "m1", "deviceId": "bridge-dev",
          "entityId": "idoso-sim", "eventTimeMs": 1000, "sequence": 1}
    ev.update(extra)
    return ev


class TestValidarEventoRetorno:
    def test_app_valido_normaliza(self):
        assert mensagens.validar_evento_retorno(ev_app(), "app") == {
            "eventType": "confirmacao.resposta", "state": "ESTOU_BEM", "messageId": "m1",
            "device_id": "cel1", "source": "app"}

    def test_wokwi_valido_normaliza_device_id(self):
        r = mensagens.validar_evento_retorno(ev_wokwi(), "wokwi")
        assert r["device_id"] == "bridge-dev" and r["source"] == "wokwi"

    def test_device_id_tem_precedencia_sobre_device_id_camel(self):
        r = mensagens.validar_evento_retorno(ev_app(deviceId="outro"), "app")
        assert r["device_id"] == "cel1"

    @pytest.mark.parametrize("tipo,state", [("push.ack", "ATIVADO"), ("push.ack", "REJEITADO"),
                                            ("confirmacao.resposta", "ESTOU_BEM"),
                                            ("confirmacao.resposta", "SEM_RESPOSTA")])
    def test_pares_validos(self, tipo, state):
        mensagens.validar_evento_retorno(ev_app(eventType=tipo, state=state), "app")

    @pytest.mark.parametrize("tipo", ["", "outro", None, "push.ACK"])
    def test_event_type_invalido(self, tipo):
        with pytest.raises(mensagens.EventoRetornoInvalido):
            mensagens.validar_evento_retorno(ev_app(eventType=tipo), "app")

    @pytest.mark.parametrize("tipo,state", [("push.ack", "ESTOU_BEM"), ("confirmacao.resposta", "ATIVADO"),
                                            ("push.ack", None), ("push.ack", "ativado")])
    def test_state_invalido_para_o_event_type(self, tipo, state):
        with pytest.raises(mensagens.EventoRetornoInvalido):
            mensagens.validar_evento_retorno(ev_app(eventType=tipo, state=state), "app")

    @pytest.mark.parametrize("mid", ["", None, 5, ["m1"]])
    def test_message_id_vazio_ou_nao_string(self, mid):
        with pytest.raises(mensagens.EventoRetornoInvalido):
            mensagens.validar_evento_retorno(ev_app(messageId=mid), "app")

    def test_message_id_ausente(self):
        ev = ev_app()
        del ev["messageId"]
        with pytest.raises(mensagens.EventoRetornoInvalido):
            mensagens.validar_evento_retorno(ev, "app")

    def test_sem_device_id_nem_device_id_camel(self):
        ev = ev_app()
        del ev["device_id"]
        with pytest.raises(mensagens.EventoRetornoInvalido):
            mensagens.validar_evento_retorno(ev, "app")

    def test_app_sem_event_time(self):
        ev = ev_app()
        del ev["event_time"]
        with pytest.raises(mensagens.EventoRetornoInvalido):
            mensagens.validar_evento_retorno(ev, "app")

    @pytest.mark.parametrize("campo", ["entityId", "eventTimeMs", "sequence"])
    def test_wokwi_sem_campo_obrigatorio(self, campo):
        ev = ev_wokwi()
        del ev[campo]
        with pytest.raises(mensagens.EventoRetornoInvalido):
            mensagens.validar_evento_retorno(ev, "wokwi")

    def test_campos_da_outra_fonte_nao_servem(self):
        with pytest.raises(mensagens.EventoRetornoInvalido):
            mensagens.validar_evento_retorno(ev_app(), "wokwi")       # falta entityId etc.
        with pytest.raises(mensagens.EventoRetornoInvalido):
            mensagens.validar_evento_retorno(ev_wokwi(), "app")       # falta event_time

    @pytest.mark.parametrize("fonte", ["", "APP", "bridge", None, "cuidador"])
    def test_source_fora_de_fontes_validas_e_erro_de_programacao(self, fonte):
        with pytest.raises(ValueError):
            mensagens.validar_evento_retorno(ev_app(), fonte)

    def test_respeita_fontes_validas_alterada(self, monkeypatch):
        monkeypatch.setattr(const, "FONTES_VALIDAS", {"app"})
        with pytest.raises(ValueError):
            mensagens.validar_evento_retorno(ev_wokwi(), "wokwi")
