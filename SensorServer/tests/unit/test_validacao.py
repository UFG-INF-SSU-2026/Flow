"""validacao.py: a validacao ESTRUTURAL da borda (envelope, roteamento, versao, telemetria, eventos de retorno).
A semantica (faixas, sentinelas) fica nas regras: tests/unit/test_regras_legadas.py."""
import pytest

import const
import validacao
from comum import contratos


# ------------------------------------------------------------------ envelope
class TestEnvelope:
    def test_valido(self):
        validacao.validar_envelope({"nonce": "a", "ciphertext": "b", "extra": 1})

    @pytest.mark.parametrize("corpo", [None, [], "texto", 5, {}, {"nonce": "a"}, {"ciphertext": "b"},
                                       {"nonce": 1, "ciphertext": "b"}, {"nonce": "a", "ciphertext": None}], ids=repr)
    def test_invalido(self, corpo):
        with pytest.raises(validacao.CorpoInvalido):
            validacao.validar_envelope(corpo)

    def test_mensagem_nao_traz_o_corpo(self):
        with pytest.raises(validacao.CorpoInvalido) as e:
            validacao.validar_envelope({"nonce": "SEGREDO"})
        assert "SEGREDO" not in str(e.value)


# ------------------------------------------------------------------ roteamento
class TestRoteamento:
    def test_valido(self):
        assert validacao.validar_roteamento({"source": "app", "user_id": 3}) == ("app", 3)
        assert validacao.validar_roteamento({"source": "wokwi", "user_id": 0}) == ("wokwi", 0)

    @pytest.mark.parametrize("source", [None, "", "APP", "bridge", 1, ["app"]], ids=repr)
    def test_source_invalida(self, source):
        with pytest.raises(validacao.CorpoInvalido, match="source"):
            validacao.validar_roteamento({"source": source, "user_id": 1})

    def test_source_ausente(self):
        with pytest.raises(validacao.CorpoInvalido, match="source"):
            validacao.validar_roteamento({"user_id": 1})

    @pytest.mark.parametrize("uid", [None, "1", True, 1.0, 1.9, [], {}], ids=repr)
    def test_user_id_invalido(self, uid):
        with pytest.raises(validacao.CorpoInvalido, match="user_id"):
            validacao.validar_roteamento({"source": "app", "user_id": uid})

    def test_source_e_conferida_antes_do_user_id(self):
        with pytest.raises(validacao.CorpoInvalido, match="source"):
            validacao.validar_roteamento({"source": "x", "user_id": "1"})

    def test_respeita_fontes_validas_alterada(self, monkeypatch):
        monkeypatch.setattr(const, "FONTES_VALIDAS", {"app"})
        with pytest.raises(validacao.CorpoInvalido):
            validacao.validar_roteamento({"source": "wokwi", "user_id": 1})


class TestLerUserId:
    @pytest.mark.parametrize("v", [0, 1, 42])
    def test_inteiro_aceito(self, v):
        assert validacao.ler_user_id(v) == v

    @pytest.mark.parametrize("v", [True, False, 1.0, "1", None, [], {}, float("nan")])
    def test_o_resto_e_rejeitado(self, v):
        assert validacao.ler_user_id(v) is None


# ------------------------------------------------------------------ versao
class TestVersao:
    def test_ausente_vale_o_padrao(self):
        assert validacao.verificar_versao({}, 3) == const.SCHEMA_VERSION_PADRAO

    @pytest.mark.parametrize("v", [1, 2, 3])
    def test_aceitas(self, v):
        assert validacao.verificar_versao({"schemaVersion": v}, 3) == v

    @pytest.mark.parametrize("v", [0, 4, -1, "3", 3.0, True, False, None, [3]], ids=repr)
    def test_rejeitadas(self, v):
        assert validacao.verificar_versao({"schemaVersion": v}, 3) is None

    def test_versao_atual_da_fonte(self, monkeypatch):
        monkeypatch.setattr(const, "SCHEMA_VERSION_APP", 7)
        monkeypatch.setattr(const, "SCHEMA_VERSION_WOKWI", 9)
        assert validacao.versao_atual_da_fonte("app") == 7 and validacao.versao_atual_da_fonte("wokwi") == 9
        with pytest.raises(ValueError):
            validacao.versao_atual_da_fonte("outra")


# ------------------------------------------------------------------ telemetria do wokwi
def ev_wk(**extra):
    ev = {"schemaVersion": const.SCHEMA_VERSION_WOKWI, "eventType": "imobilidade.leitura", "deviceId": "d1",
          "entityId": "e1", "eventTimeMs": 1000, "sequence": 1, "value": 0.3, "unit": "m/s2", "state": "NORMAL"}
    ev.update(extra)
    return ev


class TestTelemetriaWokwi:
    def test_valido(self):
        assert validacao.erros_telemetria_wokwi(ev_wk()) == []

    @pytest.mark.parametrize("campo", contratos.obrigatorios("wokwi-telemetria"))
    def test_campo_obrigatorio_ausente(self, campo):
        ev = ev_wk()
        del ev[campo]
        assert validacao.erros_telemetria_wokwi(ev)

    def test_event_type_desconhecido(self):
        assert validacao.erros_telemetria_wokwi(ev_wk(eventType="outro"))

    def test_state_de_outro_event_type(self):
        assert validacao.erros_telemetria_wokwi(ev_wk(state="ALERTA_IMOBILIDADE"))
        assert validacao.erros_telemetria_wokwi(ev_wk(eventType="imobilidade.decisao", state="NORMAL"))

    def test_value_nao_numerico(self):
        assert validacao.erros_telemetria_wokwi(ev_wk(value="1"))

    @pytest.mark.parametrize("seq", ["1", 1.5, True, None], ids=repr)
    def test_sequence_nao_inteiro(self, seq):
        assert validacao.erros_telemetria_wokwi(ev_wk(sequence=seq))

    @pytest.mark.parametrize("v", [-1.0, 500.0, 200.0])
    def test_faixa_fisica_nao_e_estrutura(self, v):
        """Sentinela, fora de faixa e valores absurdos passam: quem decide e a regra (analise)."""
        assert validacao.erros_telemetria_wokwi(ev_wk(value=v)) == []

    def test_campo_novo_desconhecido_e_aceito(self):
        assert validacao.erros_telemetria_wokwi(ev_wk(campoDeUmaVersaoFutura={"x": 1})) == []


# ------------------------------------------------------------------ leitura_ambiente
def ev_la(**extra):
    ev = {"schemaVersion": const.SCHEMA_VERSION_APP, "device_id": "d1", "event_time": "2026-10-14T22:00:00-03:00",
          "luminosidade": 10, "cct": 6000, "seq_num": 1}
    ev.update(extra)
    return ev


class TestLeituraAmbiente:
    def test_valida(self):
        assert validacao.erros_leitura_ambiente(ev_la()) == []

    @pytest.mark.parametrize("campo", ["device_id", "event_time", "luminosidade", "cct", "seq_num"])
    def test_campo_obrigatorio_ausente(self, campo):
        ev = ev_la()
        del ev[campo]
        assert validacao.erros_leitura_ambiente(ev)

    @pytest.mark.parametrize("campo,valor", [("luminosidade", "10"), ("luminosidade", None), ("cct", "6000"),
                                             ("cct", None), ("seq_num", 1.5), ("event_time", 5)], ids=repr)
    def test_tipo_errado(self, campo, valor):
        assert validacao.erros_leitura_ambiente(ev_la(**{campo: valor}))

    def test_nao_objeto(self):
        assert validacao.erros_leitura_ambiente([1, 2])

    def test_faixas_nao_sao_estrutura(self):
        assert validacao.erros_leitura_ambiente(ev_la(luminosidade=-1, cct=99999, event_time="ontem")) == []


# ------------------------------------------------------------------ eventos de retorno
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
        assert validacao.validar_evento_retorno(ev_app(), "app") == {
            "eventType": "confirmacao.resposta", "state": "ESTOU_BEM", "messageId": "m1",
            "device_id": "cel1", "source": "app"}

    def test_wokwi_valido_normaliza_device_id(self):
        r = validacao.validar_evento_retorno(ev_wokwi(), "wokwi")
        assert r["device_id"] == "bridge-dev" and r["source"] == "wokwi"

    def test_device_id_tem_precedencia_sobre_device_id_camel(self):
        assert validacao.validar_evento_retorno(ev_app(deviceId="outro"), "app")["device_id"] == "cel1"

    @pytest.mark.parametrize("tipo,state", [("push.ack", "ATIVADO"), ("push.ack", "REJEITADO"),
                                            ("confirmacao.resposta", "ESTOU_BEM"),
                                            ("confirmacao.resposta", "SEM_RESPOSTA")])
    def test_pares_validos(self, tipo, state):
        validacao.validar_evento_retorno(ev_app(eventType=tipo, state=state), "app")

    @pytest.mark.parametrize("tipo", ["", "outro", None, "push.ACK"])
    def test_event_type_invalido(self, tipo):
        with pytest.raises(validacao.EventoRetornoInvalido):
            validacao.validar_evento_retorno(ev_app(eventType=tipo), "app")

    @pytest.mark.parametrize("tipo,state", [("push.ack", "ESTOU_BEM"), ("confirmacao.resposta", "ATIVADO"),
                                            ("push.ack", None), ("push.ack", "ativado")])
    def test_state_invalido_para_o_event_type(self, tipo, state):
        with pytest.raises(validacao.EventoRetornoInvalido):
            validacao.validar_evento_retorno(ev_app(eventType=tipo, state=state), "app")

    @pytest.mark.parametrize("mid", ["", None, 5, ["m1"]], ids=repr)
    def test_message_id_vazio_ou_nao_string(self, mid):
        with pytest.raises(validacao.EventoRetornoInvalido):
            validacao.validar_evento_retorno(ev_app(messageId=mid), "app")

    def test_message_id_ausente(self):
        ev = ev_app()
        del ev["messageId"]
        with pytest.raises(validacao.EventoRetornoInvalido):
            validacao.validar_evento_retorno(ev, "app")

    def test_sem_device_id_nem_device_id_camel(self):
        ev = ev_app()
        del ev["device_id"]
        with pytest.raises(validacao.EventoRetornoInvalido):
            validacao.validar_evento_retorno(ev, "app")

    def test_device_id_vazio(self):
        with pytest.raises(validacao.EventoRetornoInvalido):
            validacao.validar_evento_retorno(ev_app(device_id=""), "app")

    def test_app_sem_event_time(self):
        ev = ev_app()
        del ev["event_time"]
        with pytest.raises(validacao.EventoRetornoInvalido):
            validacao.validar_evento_retorno(ev, "app")

    @pytest.mark.parametrize("campo", ["entityId", "eventTimeMs", "sequence"])
    def test_wokwi_sem_campo_obrigatorio(self, campo):
        ev = ev_wokwi()
        del ev[campo]
        with pytest.raises(validacao.EventoRetornoInvalido):
            validacao.validar_evento_retorno(ev, "wokwi")

    def test_campos_da_outra_fonte_nao_servem(self):
        with pytest.raises(validacao.EventoRetornoInvalido):
            validacao.validar_evento_retorno(ev_app(), "wokwi")       # falta entityId etc.
        with pytest.raises(validacao.EventoRetornoInvalido):
            validacao.validar_evento_retorno(ev_wokwi(), "app")       # falta event_time

    @pytest.mark.parametrize("fonte", ["", "APP", "bridge", None, "cuidador"], ids=repr)
    def test_source_fora_de_fontes_validas_e_erro_de_programacao(self, fonte):
        with pytest.raises(ValueError):
            validacao.validar_evento_retorno(ev_app(), fonte)

    def test_respeita_fontes_validas_alterada(self, monkeypatch):
        monkeypatch.setattr(const, "FONTES_VALIDAS", {"app"})
        with pytest.raises(ValueError):
            validacao.validar_evento_retorno(ev_wokwi(), "wokwi")

    def test_mensagem_de_erro_nao_traz_o_conteudo(self):
        with pytest.raises(validacao.EventoRetornoInvalido) as e:
            validacao.validar_evento_retorno(ev_app(state="SEGREDO-NAO-VAZAR"), "app")
        assert "SEGREDO-NAO-VAZAR" not in str(e.value)
