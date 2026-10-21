"""mensagens.py: pedido e encerramento (prazos) e envio com replay. A validacao dos eventos de retorno e da borda: test_validacao.py."""
from datetime import datetime, timedelta, timezone

import pytest

from comum import const as const_comum
from comum import mensagens

T0 = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)


def pedido(timeout_s=5, agora=T0):
    return mensagens.criar_pedido_confirmacao(1, "ep1", timeout_s, "IMPACTO_SEGUIDO_DE_IMOBILIDADE", agora=agora)


# ----------------------------- criacao ---------------------------------------
class TestCriarPedido:
    def test_campos_do_contrato(self):
        m = pedido()
        assert m["schemaVersion"] == const_comum.SCHEMA_VERSION_MENSAGEM
        assert m["messageType"] == mensagens.TIPO_SOLICITADA
        assert m["userId"] == 1 and m["episodeId"] == "ep1" and m["messageId"]
        assert m["payload"]["motivo"] == "IMPACTO_SEGUIDO_DE_IMOBILIDADE"

    def test_expires_at_e_emissao_mais_timeout_mais_margem(self):
        m = pedido(timeout_s=5)
        assert mensagens.parse_iso_z(m["issuedAt"]) == T0
        esperado = T0 + timedelta(seconds=5 + const_comum.MARGEM_SERVIDOR_S)
        assert mensagens.parse_iso_z(m["expiresAt"]) == esperado

    def test_timeout_ms_em_milissegundos(self):
        assert pedido(timeout_s=5)["payload"]["timeoutMs"] == 5000
        assert pedido(timeout_s=2.5)["payload"]["timeoutMs"] == 2500

    def test_message_id_unico(self):
        assert pedido()["messageId"] != pedido()["messageId"]

    def test_margem_vem_de_const_na_hora_da_chamada(self, monkeypatch):
        monkeypatch.setattr(const_comum, "MARGEM_SERVIDOR_S", 10)
        assert mensagens.parse_iso_z(pedido()["expiresAt"]) == T0 + timedelta(seconds=15)


class TestCriarEncerramento:
    @pytest.mark.parametrize("resultado", ["CONFIRMADO", "EXPIRADO", "CANCELADO"])
    def test_resultados_validos(self, resultado):
        m = mensagens.criar_encerramento(1, "ep1", resultado, agora=T0)
        assert m["messageType"] == mensagens.TIPO_ENCERRADA
        assert m["payload"] == {"resultado": resultado}

    def test_ttl_de_10_segundos(self):
        m = mensagens.criar_encerramento(1, "ep1", "EXPIRADO", agora=T0)
        assert mensagens.parse_iso_z(m["expiresAt"]) == T0 + timedelta(seconds=const_comum.TTL_ENCERRAMENTO_S)
        assert const_comum.TTL_ENCERRAMENTO_S == 10

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
        depois = T0 + timedelta(seconds=5 + const_comum.MARGEM_SERVIDOR_S)    # exatamente em expiresAt
        assert mensagens.para_envio(m, agora=depois) is None

    def test_encerramento_expirado_devolve_none(self):
        m = mensagens.criar_encerramento(1, "ep1", "EXPIRADO", agora=T0)
        assert mensagens.para_envio(m, agora=T0 + timedelta(seconds=const_comum.TTL_ENCERRAMENTO_S + 1)) is None

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
        dentro_da_margem = T0 + timedelta(seconds=5 + const_comum.MARGEM_SERVIDOR_S - 0.5)
        assert mensagens.parse_iso_z(m["expiresAt"]) > dentro_da_margem
        assert mensagens.para_envio(m, agora=dentro_da_margem) is None

    def test_timeout_nunca_passa_do_original(self):
        copia = mensagens.para_envio(pedido(), agora=T0 - timedelta(seconds=1))   # relogio um pouco atras
        assert copia["payload"]["timeoutMs"] == 5000
