"""regra_imobilidade e regra_luz: validacao, deduplicacao, estado por (user_id, device_id) e atuacao."""
import pytest

import regra_imobilidade as imob
import regra_luz as luz


def ev_imob(seq=1, event_type="imobilidade.leitura", state="NORMAL", value=0.3, device="d1", **extra):
    ev = {"schemaVersion": 3, "eventType": event_type, "deviceId": device, "entityId": "e1",
          "eventTimeMs": seq * 1000, "sequence": seq, "value": value, "unit": "m/s2", "state": state}
    ev.update(extra)
    return ev


def decisao(state, seq, **extra):
    return ev_imob(seq=seq, event_type="imobilidade.decisao", state=state, **extra)


class TestImobilidadeValidacao:
    def test_evento_valido(self):
        imob.validar_evento(ev_imob())

    @pytest.mark.parametrize("campo", imob.CAMPOS_OBRIGATORIOS)
    def test_campo_obrigatorio_ausente(self, campo):
        ev = ev_imob()
        del ev[campo]
        with pytest.raises(imob.EventoInvalido):
            imob.validar_evento(ev)

    def test_event_type_desconhecido(self):
        with pytest.raises(imob.EventoInvalido):
            imob.validar_evento(ev_imob(event_type="outro"))

    def test_state_de_outro_event_type(self):
        with pytest.raises(imob.EventoInvalido):
            imob.validar_evento(ev_imob(state="ALERTA_IMOBILIDADE"))

    def test_value_nao_numerico(self):
        with pytest.raises(imob.EventoInvalido):
            imob.validar_evento(ev_imob(value="1"))

    @pytest.mark.parametrize("seq", ["1", 1.5, True, None])
    def test_sequence_nao_inteiro(self, seq):
        ev = ev_imob()
        ev["sequence"] = seq
        with pytest.raises(imob.EventoInvalido):
            imob.validar_evento(ev)

    @pytest.mark.parametrize("v", [-0.1, 160.1])
    def test_fora_da_faixa_fisica(self, v):
        with pytest.raises(imob.EventoInvalido):
            imob.validar_evento(ev_imob(value=v))

    def test_leitura_invalida_exige_o_sentinela(self):
        imob.validar_evento(decisao("LEITURA_INVALIDA", 1, value=-1.0))
        with pytest.raises(imob.EventoInvalido):
            imob.validar_evento(decisao("LEITURA_INVALIDA", 1, value=0.5))

    def test_fora_de_faixa_exige_valor_acima_do_teto(self):
        imob.validar_evento(decisao("LEITURA_FORA_DE_FAIXA", 1, value=200.0))
        with pytest.raises(imob.EventoInvalido):
            imob.validar_evento(decisao("LEITURA_FORA_DE_FAIXA", 1, value=50.0))


class TestImobilidadeEstado:
    def test_duplicado_por_sequence(self):
        assert imob.eh_duplicado(1, ev_imob(seq=5)) is False
        assert imob.eh_duplicado(1, ev_imob(seq=5)) is True
        assert imob.eh_duplicado(1, ev_imob(seq=4)) is True
        assert imob.eh_duplicado(1, ev_imob(seq=6)) is False

    def test_alerta_so_uma_vez_e_rearma_com_estado_que_encerra(self):
        assert imob.processar_evento(1, decisao("ALERTA_IMOBILIDADE", 1))["tipo"] == "alerta_imobilidade"
        assert imob.processar_evento(1, decisao("ALERTA_IMOBILIDADE", 2)) is None
        assert imob.processar_evento(1, decisao("SUSPEITA_IMOBILIDADE", 3)) is None      # nao rearma
        assert imob.processar_evento(1, decisao("ALERTA_IMOBILIDADE", 4)) is None
        assert imob.processar_evento(1, decisao("MOVIMENTO_RETOMADO", 5)) is None       # rearma
        assert imob.processar_evento(1, decisao("ALERTA_IMOBILIDADE", 6)) is not None

    def test_leitura_so_atualiza_o_estado(self):
        assert imob.processar_evento(1, ev_imob(seq=1, state="AGUARDANDO_CONFIRMACAO")) is None
        est = imob.obter_estado("d1")
        assert est["estado_atual"] == "AGUARDANDO_CONFIRMACAO" and est["alerta_ativo"] is False

    def test_obter_estado_desconhecido(self):
        assert imob.obter_estado("nao-existe") is None

    def test_estado_e_por_usuario(self):
        imob.eh_duplicado(1, ev_imob(seq=10))
        # mesmo device_id em outro usuario: sequence independente
        assert imob.eh_duplicado(2, ev_imob(seq=1)) is False
        imob.processar_evento(1, decisao("ALERTA_IMOBILIDADE", 11))
        assert imob.processar_evento(2, decisao("ALERTA_IMOBILIDADE", 2)) is not None   # alerta_ativo nao vazou

    def test_obter_estado_devolve_o_do_usuario_atualizado_por_ultimo(self):
        imob.processar_evento(1, ev_imob(seq=1))
        imob.processar_evento(2, ev_imob(seq=1, state="ALERTA_CONFIRMADO"))
        assert imob.obter_estado("d1")["user_id"] == 2
        imob.processar_evento(1, ev_imob(seq=2))
        est = imob.obter_estado("d1")
        assert est["user_id"] == 1 and est["estado_atual"] == "NORMAL"


def ev_luz(seq=1, cct=6000, hora="22:00:00", device="d1", lux=10, **extra):
    ev = {"schemaVersion": 3, "device_id": device, "event_time": f"2026-10-14T{hora}-03:00",
          "luminosidade": lux, "cct": cct, "seq_num": seq}
    ev.update(extra)
    return ev


class TestLuzValidacao:
    def test_evento_valido(self):
        luz.validar_evento(ev_luz())

    @pytest.mark.parametrize("campo", ["device_id", "event_time", "luminosidade", "cct", "seq_num"])
    def test_campo_obrigatorio_ausente(self, campo):
        ev = ev_luz()
        del ev[campo]
        with pytest.raises(luz.EventoInvalido):
            luz.validar_evento(ev)

    @pytest.mark.parametrize("lux", [-1, "10", None])
    def test_luminosidade_invalida(self, lux):
        with pytest.raises(luz.EventoInvalido):
            luz.validar_evento(ev_luz(lux=lux))

    @pytest.mark.parametrize("cct", [999, 12001, "6000", None])
    def test_cct_fora_de_faixa(self, cct):
        with pytest.raises(luz.EventoInvalido):
            luz.validar_evento(ev_luz(cct=cct))

    def test_event_time_invalido(self):
        with pytest.raises(luz.EventoInvalido):
            luz.validar_evento(ev_luz(event_time="ontem"))


class TestLuzRegra:
    def test_duplicado_por_seq_num_e_por_usuario(self):
        assert luz.eh_duplicado(1, ev_luz(seq=3)) is False
        assert luz.eh_duplicado(1, ev_luz(seq=3)) is True
        assert luz.eh_duplicado(2, ev_luz(seq=3)) is False       # outro usuario, mesmo device_id

    def test_debounce_de_tres_leituras(self):
        assert luz.avaliar_regra(1, ev_luz(seq=1)) is None
        assert luz.avaliar_regra(1, ev_luz(seq=2)) is None
        alerta = luz.avaliar_regra(1, ev_luz(seq=3))
        assert alerta["tipo"] == "alerta_luz_inadequada"
        assert luz.avaliar_regra(1, ev_luz(seq=4)) is None        # nao repete no mesmo episodio

    def test_condicao_desfeita_zera_e_permite_novo_episodio(self):
        for s in (1, 2, 3):
            luz.avaliar_regra(1, ev_luz(seq=s))
        assert luz.avaliar_regra(1, ev_luz(seq=4, cct=3000)) is None
        assert [luz.avaliar_regra(1, ev_luz(seq=s)) is not None for s in (5, 6, 7)] == [False, False, True]

    def test_contador_nao_vaza_entre_usuarios(self):
        luz.avaliar_regra(1, ev_luz(seq=1))
        luz.avaliar_regra(1, ev_luz(seq=2))
        assert luz.avaliar_regra(2, ev_luz(seq=3)) is None        # contador do usuario 2 comeca do zero

    @pytest.mark.parametrize("hora,alerta", [("18:00:00", True), ("23:59:00", True), ("05:59:00", True),
                                             ("06:00:00", False), ("12:00:00", False), ("17:59:00", False)])
    def test_janela_noturna(self, hora, alerta):
        r = [luz.avaliar_regra(1, ev_luz(seq=s, hora=hora)) for s in (1, 2, 3)]
        assert (r[2] is not None) is alerta

    def test_cct_no_limiar_nao_e_frio(self):
        import const
        r = [luz.avaliar_regra(1, ev_luz(seq=s, cct=const.CCT_LIMIAR_FRIO)) for s in (1, 2, 3)]
        assert r == [None, None, None]
