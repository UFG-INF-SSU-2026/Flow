"""
nucleo: "processar este lote com este relogio e este estado" (S2a do roadmap do split).

Sem Flask, sem thread, sem banco e sem sleep: cada leitura traz o seu instante (`em(t)`), e o nucleo e quem
decide. Os efeitos (arquivo, banco, push) so acontecem quando o teste chama `.aplicar()` de uma decisao.
"""
from datetime import datetime, timedelta, timezone

import pytest

from comum import const as const_comum
from analise import const as const_analise
from comum import mensagens
from analise import nucleo
from analise import regra_imobilidade
from analise import regra_queda
from borda import tempo

T0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
TS_APP = 1_760_000_000_000


def em(segundos: float) -> datetime:
    return T0 + timedelta(seconds=segundos)


# ------------------------------------------------------------------ fabricas de entrada
def wokwi(seq, value, vmax=None, state="NORMAL", evento="imobilidade.leitura", **extra) -> dict:
    """Payload do wokwi como fica gravado em `leituras` (sem `source` e `user_id`)."""
    p = {"schemaVersion": const_comum.SCHEMA_VERSION_WOKWI, "eventType": evento, "deviceId": "bridge-dev",
         "entityId": "idoso-sim", "eventTimeMs": seq * 1000, "sequence": seq, "value": value, "unit": "m/s2",
         "state": state}
    if vmax is not None:
        p["valueMax"] = vmax
    p.update(extra)
    return p


def leitura_wokwi(t, seq, value, vmax=None, user_id=1, **kw) -> nucleo.Leitura:
    return nucleo.Leitura(em(t), user_id, "bridge-dev", "wokwi", wokwi(seq, value, vmax, **kw),
                          const_comum.SCHEMA_VERSION_WOKWI)


def leitura_app(t, magnitude, maximo, user_id=1, **extra) -> nucleo.Leitura:
    payload = {"device_id": "cel1", "timestamp_envio": TS_APP + int(t * 1000),
               "acelerometro": {"magnitude": magnitude, "magnitude_max_janela": maximo}}
    payload.update(extra)
    return nucleo.Leitura(em(t), user_id, "cel1", "app", payload, const_comum.SCHEMA_VERSION_APP)


def ambiente(t, seq, cct=6500) -> nucleo.Leitura:
    bloco = {"schemaVersion": const_comum.SCHEMA_VERSION_APP, "device_id": "cel1", "event_time": "2026-10-14T22:00:00+00:00",
             "luminosidade": 5.0, "cct": cct, "seq_num": seq}
    return nucleo.Leitura(em(t), 1, "cel1", "app", {"device_id": "cel1", "leitura_ambiente": bloco},
                          const_comum.SCHEMA_VERSION_APP)


def retorno(message_id, user_id=1, tipo="confirmacao.resposta", state="ESTOU_BEM", t=6) -> nucleo.Resposta:
    ev = {"eventType": tipo, "state": state, "messageId": message_id, "device_id": "cel1", "source": "app"}
    return nucleo.Resposta(em(t), user_id, "cel1", "app", ev, const_comum.SCHEMA_VERSION_APP)


# ------------------------------------------------------------------ cenarios
def queda_wokwi(estado, t0=0, seq0=1) -> list[nucleo.Decisoes]:
    """Leitura normal, impacto e T_POS_IMPACTO_S segundos de repouso: o pedido sai na ultima (instante t0 + 4)."""
    leituras = [leitura_wokwi(t0, seq0, 1.0, 1.5), leitura_wokwi(t0 + 1, seq0 + 1, 3.0, 25.0)]
    leituras += [leitura_wokwi(t0 + 2 + k, seq0 + 2 + k, 0.3, 0.4) for k in range(int(const_analise.T_POS_IMPACTO_S))]
    return [nucleo.processar_leitura(estado, l) for l in leituras]


def resumo(d: nucleo.Decisoes) -> dict:
    """O que importa comparar entre dois processamentos (sem os ids sorteados: messageId e episodeId)."""
    return {
        "comandos": [(c.mensagem["messageType"], c.mensagem["issuedAt"], c.mensagem["expiresAt"], c.criado_em,
                      c.expira_em, c.episodio.estado, c.episodio.tipo, c.episodio.device_id) for c in d.comandos],
        "alertas": [(a.tipo, a.device_id, a.instante, a.dados.get("mensagem")) for a in d.alertas],
        "confirmacoes": [(c.tipo, c.instante) for c in d.confirmacoes],
        "estados": [(r.device_id, r.instante, r.assinatura) for r in d.estados],
        "descartes": [(x.motivo, x.detalhe) for x in d.descartes],
    }


@pytest.fixture
def sem_relogio(monkeypatch):
    """Qualquer leitura de relogio global vira erro: o nucleo so pode usar os instantes que recebe."""
    def proibido(*_a, **_k):
        raise AssertionError("o nucleo leu o relogio global")
    monkeypatch.setattr(regra_queda, "_agora", proibido)
    monkeypatch.setattr(regra_imobilidade, "_agora_iso", proibido)
    monkeypatch.setattr(mensagens, "agora_utc", proibido)
    monkeypatch.setattr(tempo, "agora_utc", proibido)


# ============================================================================ gatilho A
class TestQuedaPorImpacto:
    def test_wokwi_impacto_e_repouso_abrem_o_pedido_no_instante_da_ultima_leitura(self, sem_relogio):
        estado = nucleo.novo_estado()
        saidas = queda_wokwi(estado)
        assert [bool(s.comandos) for s in saidas] == [False, False, False, False, True]
        (cmd,) = saidas[-1].comandos
        prazo = timedelta(seconds=const_comum.TIMEOUT_CONFIRMACAO_S + const_comum.MARGEM_SERVIDOR_S)
        assert cmd.mensagem["messageType"] == mensagens.TIPO_SOLICITADA
        assert cmd.mensagem["issuedAt"] == mensagens.iso_z(em(4))
        assert cmd.mensagem["expiresAt"] == mensagens.iso_z(em(4) + prazo)
        assert (cmd.criado_em, cmd.expira_em) == (em(4), em(4) + prazo)
        ep = cmd.episodio
        assert (ep.episodio_id, ep.estado, ep.tipo, ep.device_id) == (
            cmd.mensagem["episodeId"], "aguardando_confirmacao", const_analise.MOTIVO_A, "bridge-dev")
        assert (ep.aberto_em, ep.atualizado_em, ep.encerrado_em) == (em(4), em(4), None)
        assert estado.queda.episodios[1].prazo == em(4).timestamp() + const_comum.TIMEOUT_CONFIRMACAO_S + const_comum.MARGEM_SERVIDOR_S

    def test_o_registro_episodio_aberto_vira_decisao_com_o_instante_da_leitura(self, sem_relogio):
        saidas = queda_wokwi(nucleo.novo_estado())
        (aberto,) = saidas[-1].confirmacoes
        assert (aberto.tipo, aberto.instante) == ("episodio.aberto", em(4))
        assert aberto.dados["motivo"] == const_analise.MOTIVO_A and aberto.dados["fonte_do_disparo"] == "wokwi"

    def test_app_tambem_abre_pelo_gatilho_a(self, sem_relogio):
        estado = nucleo.novo_estado()
        saidas = [nucleo.processar_leitura(estado, l) for l in (
            leitura_app(0, 9.81, 10.0), leitura_app(1, 20.0, 25.81), leitura_app(2, 9.81, 10.0),
            leitura_app(3, 9.81, 10.0), leitura_app(4, 9.81, 10.0))]
        (cmd,) = [c for s in saidas for c in s.comandos]
        assert cmd.mensagem["payload"]["motivo"] == const_analise.MOTIVO_A and cmd.episodio.device_id == "cel1"

    def test_leitura_duplicada_do_app_nao_alimenta_a_regra(self, sem_relogio):
        estado = nucleo.novo_estado()
        primeira = leitura_app(0, 20.0, 25.81)
        nucleo.processar_leitura(estado, primeira)
        d = nucleo.processar_leitura(estado, nucleo.Leitura(em(1), 1, "cel1", "app", primeira.payload,
                                                            primeira.schema_version))     # mesmo timestamp_envio
        assert estado.queda.estados[1].fase == "POS_IMPACTO" and not d.comandos
        assert estado.queda.estados[1].t_ultima_leitura == em(0).timestamp()          # a repetida nao moveu a maquina


# ============================================================================ gatilho B e imobilidade
class TestImobilidade:
    def test_alerta_de_imobilidade_gera_alerta_estado_e_pedido_do_gatilho_b(self, sem_relogio):
        estado = nucleo.novo_estado()
        d = nucleo.processar_leitura(estado, leitura_wokwi(10, 1, 0.3, state="ALERTA_IMOBILIDADE",
                                                           evento="imobilidade.decisao"))
        (alerta,) = d.alertas
        assert (alerta.tipo, alerta.device_id, alerta.instante) == ("imobilidade", "bridge-dev", em(10))
        assert alerta.schema_version == const_comum.SCHEMA_VERSION_WOKWI and alerta.dados["tipo"] == "alerta_imobilidade"
        assert alerta.como_linha()["mensagem"] == alerta.dados["mensagem"]
        (cmd,) = d.comandos
        assert cmd.episodio.tipo == const_analise.MOTIVO_B
        (r,) = d.estados
        assert r.estado["alerta_ativo"] is True and r.estado["ultima_decisao"] == "ALERTA_IMOBILIDADE"
        assert r.estado["ultima_atualizacao"] == em(10).isoformat()                  # o instante da leitura, nao "agora"
        assert r.como_linha()["atualizado_em"] == em(10)

    def test_o_mesmo_alerta_nao_repete_no_mesmo_episodio(self, sem_relogio):
        estado = nucleo.novo_estado()
        nucleo.processar_leitura(estado, leitura_wokwi(10, 1, 0.3, state="ALERTA_IMOBILIDADE", evento="imobilidade.decisao"))
        d = nucleo.processar_leitura(estado, leitura_wokwi(11, 2, 0.3, state="ALERTA_IMOBILIDADE", evento="imobilidade.decisao"))
        assert not d.alertas and not d.comandos

    def test_evento_duplicado_e_descartado(self):
        estado = nucleo.novo_estado()
        nucleo.processar_leitura(estado, leitura_wokwi(0, 5, 0.3, 0.4))
        d = nucleo.processar_leitura(estado, leitura_wokwi(1, 5, 0.3, 0.4))
        assert [x.motivo for x in d.descartes] == [nucleo.DESCARTE_DUPLICADA] and not d.estados

    def test_estrutura_invalida_e_descartada_sem_tocar_o_estado(self):
        estado = nucleo.novo_estado()
        ruim = wokwi(1, 0.3, 0.4)
        del ruim["entityId"]
        d = nucleo.processar_leitura(estado, nucleo.Leitura(em(0), 1, "bridge-dev", "wokwi", ruim,
                                                            const_comum.SCHEMA_VERSION_WOKWI))
        assert [x.motivo for x in d.descartes] == [nucleo.DESCARTE_ESTRUTURA]
        assert estado.imobilidade.estados == {} and estado.queda.estados == {}

    def test_valor_fora_da_faixa_fisica_e_descartado_pela_semantica(self):
        d = nucleo.processar_leitura(nucleo.novo_estado(), leitura_wokwi(0, 1, 500.0, 500.0))
        assert [x.motivo for x in d.descartes] == [nucleo.DESCARTE_SEMANTICA] and not d.estados

    def test_falha_inesperada_na_regra_conta_como_nao_aceito(self, monkeypatch):
        def quebra(*_a, **_k):
            raise RuntimeError("bug")
        monkeypatch.setattr(nucleo, "_regra_de_imobilidade", quebra)
        d = nucleo.processar_leitura(nucleo.novo_estado(), leitura_wokwi(0, 1, 0.3, 0.4))
        assert [x.motivo for x in d.descartes] == [nucleo.DESCARTE_FALHA] and not d.estados and not d.comandos


# ============================================================================ regra de luz
class TestLuz:
    def test_tres_leituras_noturnas_frias_geram_o_alerta_no_instante_da_terceira(self, sem_relogio):
        estado = nucleo.novo_estado()
        saidas = [nucleo.processar_leitura(estado, ambiente(t, seq=t + 1)) for t in range(3)]
        assert [len(s.alertas) for s in saidas] == [0, 0, 1]
        (alerta,) = saidas[-1].alertas
        assert (alerta.tipo, alerta.instante, alerta.schema_version) == ("luz_inadequada", em(2), const_comum.SCHEMA_VERSION_APP)
        assert alerta.dados["tipo"] == "alerta_luz_inadequada"

    def test_seq_num_repetido_e_descartado(self):
        estado = nucleo.novo_estado()
        nucleo.processar_leitura(estado, ambiente(0, seq=1))
        d = nucleo.processar_leitura(estado, ambiente(1, seq=1))
        assert [x.motivo for x in d.descartes] == [nucleo.DESCARTE_DUPLICADA]

    def test_falha_na_regra_de_luz_nao_impede_a_regra_de_queda(self, monkeypatch):
        def quebra(*_a, **_k):
            raise RuntimeError("bug")
        monkeypatch.setattr(nucleo, "_regra_de_luz", quebra)
        estado = nucleo.novo_estado()
        saidas = [nucleo.processar_leitura(estado, l) for l in (
            leitura_app(0, 20.0, 25.81), leitura_app(1, 9.81, 10.0), leitura_app(2, 9.81, 10.0),
            leitura_app(3, 9.81, 10.0))]
        assert len(saidas[-1].comandos) == 1
        assert [x.motivo for x in saidas[0].descartes] == [nucleo.DESCARTE_FALHA]


# ============================================================================ respostas e vigia
class TestRespostaEVigia:
    def _abrir(self, estado):
        (cmd,) = queda_wokwi(estado)[-1].comandos
        return cmd

    def test_estou_bem_encerra_como_confirmado_e_gera_o_comando_de_encerramento(self, sem_relogio):
        estado = nucleo.novo_estado()
        aberto = self._abrir(estado)
        d = nucleo.processar_resposta(estado, retorno(aberto.mensagem["messageId"], t=6))
        assert [c.tipo for c in d.confirmacoes] == ["confirmacao.resposta", "episodio.encerrado"]
        assert all(c.instante == em(6) for c in d.confirmacoes)
        (cmd,) = d.comandos
        assert cmd.mensagem["messageType"] == mensagens.TIPO_ENCERRADA
        assert cmd.mensagem["payload"]["resultado"] == "CONFIRMADO" and cmd.mensagem["issuedAt"] == mensagens.iso_z(em(6))
        ep = cmd.episodio
        assert (ep.estado, ep.motivo_encerramento, ep.encerrado_em, ep.tipo) == ("encerrado", "CONFIRMADO", em(6), "DESCONHECIDO")
        assert cmd.expira_em is None and cmd.como_linha()["episodio_id"] == aberto.episodio.episodio_id

    def test_resposta_de_outro_usuario_nao_encerra_nada(self):
        estado = nucleo.novo_estado()
        aberto = self._abrir(estado)
        d = nucleo.processar_resposta(estado, retorno(aberto.mensagem["messageId"], user_id=2))
        assert not d.comandos
        assert [c.dados.get("mensagem_desconhecida") for c in d.confirmacoes] == [True]
        assert estado.queda.episodios[1].encerrado_em is None

    def test_push_ack_so_registra(self):
        estado = nucleo.novo_estado()
        aberto = self._abrir(estado)
        d = nucleo.processar_resposta(estado, retorno(aberto.mensagem["messageId"], tipo="push.ack", state="ATIVADO"))
        assert [c.tipo for c in d.confirmacoes] == ["push.ack"] and not d.comandos

    def test_vigia_expira_so_no_prazo_do_servidor(self, sem_relogio):
        estado = nucleo.novo_estado()
        aberto = self._abrir(estado)
        prazo = em(4) + timedelta(seconds=const_comum.TIMEOUT_CONFIRMACAO_S + const_comum.MARGEM_SERVIDOR_S)
        assert not nucleo.vigiar(estado, prazo - timedelta(milliseconds=10))
        d = nucleo.vigiar(estado, prazo)
        (cmd,) = d.comandos
        assert cmd.mensagem["payload"]["resultado"] == "EXPIRADO" and cmd.episodio.motivo_encerramento == "EXPIRADO"
        assert (cmd.criado_em, cmd.episodio.encerrado_em) == (prazo, prazo)
        assert [(c.tipo, c.instante) for c in d.confirmacoes] == [("episodio.encerrado", prazo)]
        assert cmd.episodio.episodio_id == aberto.episodio.episodio_id
        assert not nucleo.vigiar(estado, prazo + timedelta(seconds=1))                # ja encerrado: nao repete

    def test_cooldown_conta_a_partir_do_encerramento_no_relogio_recebido(self, sem_relogio):
        estado = nucleo.novo_estado()
        self._abrir(estado)
        prazo = em(4) + timedelta(seconds=const_comum.TIMEOUT_CONFIRMACAO_S + const_comum.MARGEM_SERVIDOR_S)
        nucleo.vigiar(estado, prazo)
        antes = [nucleo.processar_leitura(estado, l) for l in queda_wokwi_leituras(t0=12, seq0=100)]
        assert not any(s.comandos for s in antes)                                      # dentro do cooldown
        depois = [nucleo.processar_leitura(estado, l) for l in
                  queda_wokwi_leituras(t0=(prazo - T0).total_seconds() + const_analise.COOLDOWN_S, seq0=200)]
        assert sum(len(s.comandos) for s in depois) == 1


def queda_wokwi_leituras(t0, seq0):
    leituras = [leitura_wokwi(t0, seq0, 1.0, 1.5), leitura_wokwi(t0 + 1, seq0 + 1, 3.0, 25.0)]
    leituras += [leitura_wokwi(t0 + 2 + k, seq0 + 2 + k, 0.3, 0.4) for k in range(int(const_analise.T_POS_IMPACTO_S))]
    return leituras


# ============================================================================ estado, lote, efeitos
class TestEstadoELote:
    def test_estados_novos_sao_independentes_entre_si_e_do_padrao(self):
        a, b = nucleo.novo_estado(), nucleo.novo_estado()
        queda_wokwi(a)
        assert len(a.queda.episodios) == 1
        assert b.queda.episodios == {} and regra_queda.PADRAO.episodios == {} and nucleo.PADRAO.queda.episodios == {}

    def test_padrao_e_as_tabelas_dos_modulos_de_regra(self):
        assert nucleo.PADRAO.queda.episodios is regra_queda._episodios
        assert nucleo.PADRAO.imobilidade.estados is regra_imobilidade._estados

    def test_mesma_sequencia_em_estados_diferentes_da_as_mesmas_decisoes(self):
        """A base da comparacao em sombra (S3): o resultado depende so da entrada e dos instantes."""
        saidas = [[resumo(d) for d in queda_wokwi(est)] for est in (nucleo.novo_estado(), nucleo.novo_estado(), nucleo.PADRAO)]
        assert saidas[0] == saidas[1] == saidas[2]

    def test_lote_desordenado_e_processado_em_ordem_de_instante(self):
        leituras = queda_wokwi_leituras(t0=0, seq0=1)
        ordenado = nucleo.processar_lote(nucleo.novo_estado(), leituras)
        embaralhado = nucleo.processar_lote(nucleo.novo_estado(), list(reversed(leituras)))
        assert len(ordenado.comandos) == 1 and resumo(ordenado) == resumo(embaralhado)

    def test_lote_com_item_desconhecido_e_erro_de_programacao(self):
        with pytest.raises(TypeError):
            nucleo.processar_lote(nucleo.novo_estado(), [object()])

    def test_instante_sem_fuso_e_recusado(self):
        with pytest.raises(ValueError):
            nucleo.Leitura(datetime(2026, 10, 14, 12, 0, 0), 1, "d", "wokwi", {}, 3)
        with pytest.raises(ValueError):
            nucleo.vigiar(nucleo.novo_estado(), datetime(2026, 10, 14, 12, 0, 0))

    def test_o_nucleo_nao_faz_io_e_aplicar_grava_com_o_instante_da_decisao(self, ler_confirmacoes, banco):
        saidas = queda_wokwi(nucleo.novo_estado())
        assert ler_confirmacoes(1) == [] and banco.tabelas["confirmacoes"] == []        # nada gravado ainda
        (aberto,) = saidas[-1].confirmacoes
        aberto.aplicar()
        (linha,) = ler_confirmacoes(1)
        assert linha["tipo"] == "episodio.aberto" and linha["timestamp"] == em(4).isoformat()
        assert [c["tipo"] for c in banco.tabelas["confirmacoes"]] == ["episodio.aberto"]
