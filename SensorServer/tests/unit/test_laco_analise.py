"""
Laco da analise por cursor. Banco falso (sem Docker), relogio explicito e ciclos SINCRONOS
(`executar_ciclo`): so o teste da thread espera de verdade.

Cobre: conversao das linhas, os tres cursores e o batimento, a transacao unica, lacuna de id, leitura malformada,
falha numa regra, "so registra" (nenhuma tabela de saida escrita), estado proprio (nao o PADRAO), idade
maxima, vigia, ownership da resposta e a thread (inicia, para, sobrevive a falha do banco sem inundar o log).
"""
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from analise import const as const_analise
from analise import laco, nucleo
from comum import const as const_comum
from comum import mensagens
from flow_db import ciclo_analise
from flow_db.fake import BancoFalso

T0 = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)
TABELAS_DE_SAIDA = ("comandos_push", "alertas", "confirmacoes", "episodios", "estado_dispositivo")


class Relogio:
    def __init__(self, agora=T0):
        self.agora = agora

    def __call__(self):
        return self.agora

    def ir_para(self, segundos: float):
        self.agora = T0 + timedelta(seconds=segundos)


# ------------------------------------------------------------------ fabricas de linhas
def _payload_wokwi(seq, value, vmax=None, state="NORMAL", evento="imobilidade.leitura"):
    p = {"schemaVersion": const_comum.SCHEMA_VERSION_WOKWI, "eventType": evento, "deviceId": "bridge-dev",
         "entityId": "idoso-sim", "eventTimeMs": seq * 1000, "sequence": seq, "value": value, "unit": "m/s2",
         "state": state}
    if vmax is not None:
        p["valueMax"] = vmax
    return p


def leitura_wokwi(t, seq, value, vmax=None, user_id=1):
    return {"instante_ingestao": T0 + timedelta(seconds=t), "user_id": user_id, "device_id": "bridge-dev",
            "fonte": "wokwi", "tipo_evento": "imobilidade.leitura", "schema_version": const_comum.SCHEMA_VERSION_WOKWI,
            "instante_origem": None, "conta_id": "bridge1", "payload": _payload_wokwi(seq, value, vmax)}


def leitura_app(t, seq, user_id=1):
    return {"instante_ingestao": T0 + timedelta(seconds=t), "user_id": user_id, "device_id": "cel1", "fonte": "app",
            "tipo_evento": "telemetria", "schema_version": const_comum.SCHEMA_VERSION_APP, "instante_origem": None,
            "conta_id": "idoso1",
            "payload": {"device_id": "cel1", "timestamp_envio": 1_760_000_000_000 + seq * 1000,
                        "acelerometro": {"magnitude": 9.8, "magnitude_max_janela": 9.9}}}


def resposta(t, message_id, user_id=1, tipo="confirmacao.resposta", state="ESTOU_BEM"):
    ev = {"eventType": tipo, "state": state, "messageId": message_id, "device_id": "cel1", "source": "app"}
    return {"instante_ingestao": T0 + timedelta(seconds=t), "user_id": user_id, "device_id": "cel1", "fonte": "app",
            "tipo": tipo, "message_id": message_id, "schema_version": const_comum.SCHEMA_VERSION_APP, "payload": ev}


def entrega(t, resultado="enviado", user_id=1):
    return {"instante": T0 + timedelta(seconds=t), "comando_id": None, "message_id": "m1", "user_id": user_id,
            "device_id": None, "canal": "app", "resultado": resultado, "detalhe": resultado}


def queda_wokwi(t0=0, seq0=1):
    """Leitura normal, impacto e T_POS_IMPACTO_S segundos de repouso: o pedido sai na ultima (instante t0 + 4)."""
    linhas = [leitura_wokwi(t0, seq0, 1.0, 1.5), leitura_wokwi(t0 + 1, seq0 + 1, 3.0, 25.0)]
    linhas += [leitura_wokwi(t0 + 2 + k, seq0 + 2 + k, 0.3, 0.4) for k in range(int(const_analise.T_POS_IMPACTO_S))]
    return linhas


# ------------------------------------------------------------------ montagem
@pytest.fixture
def mundo():
    rel = Relogio()
    bd = BancoFalso(relogio=rel)
    analise, borda = bd.analise(), bd.borda()
    decisoes: list[tuple] = []     # (Decisoes, origem, reconstrucao)
    lc = laco.LacoAnalise(obter_repo=lambda: analise, relogio=rel,
                          ao_decidir=lambda d, origem, rec: decisoes.append((d, origem, rec)))
    return SimpleNamespace(rel=rel, bd=bd, borda=borda, analise=analise, laco=lc, decisoes=decisoes)


def metricas(m) -> dict:
    return m.bd.cursor_analise["metricas"]


def comandos(m, tipo=None):
    return [c for d, _o, _r in m.decisoes for c in d.comandos if tipo is None or c.mensagem["messageType"] == tipo]


def saidas_vazias(m) -> bool:
    return all(not m.bd.tabelas[t] for t in TABELAS_DE_SAIDA)


# ============================================================================ conversao das linhas
class TestConversaoDeLinhas:
    def test_leitura_valida_vira_entrada_do_nucleo(self):
        l = laco.leitura_de_linha({"id": 1, **leitura_wokwi(2, 1, 1.0, 1.5)})
        assert isinstance(l, nucleo.Leitura)
        assert (l.instante, l.user_id, l.device_id, l.fonte, l.schema_version) == (
            T0 + timedelta(seconds=2), 1, "bridge-dev", "wokwi", const_comum.SCHEMA_VERSION_WOKWI)

    def test_resposta_valida_vira_entrada_do_nucleo_e_aceita_device_nulo(self):
        r = laco.resposta_de_linha({"id": 1, **{**resposta(3, "m1"), "device_id": None}})
        assert isinstance(r, nucleo.Resposta) and r.device_id is None and r.evento["messageId"] == "m1"

    @pytest.mark.parametrize("mudanca", [
        {"payload": ["nao", "e", "objeto"]}, {"payload": None}, {"device_id": None}, {"device_id": ""},
        {"fonte": "outra"}, {"user_id": "1"}, {"user_id": True}, {"user_id": None},
        {"schema_version": None}, {"schema_version": "3"},
        {"instante_ingestao": datetime(2026, 10, 21, 12, 0, 0)}, {"instante_ingestao": "2026-10-21"},
    ], ids=repr)
    def test_leitura_malformada_levanta(self, mudanca):
        with pytest.raises(laco.LinhaMalformada):
            laco.leitura_de_linha({"id": 1, **leitura_wokwi(0, 1, 1.0), **mudanca})

    @pytest.mark.parametrize("mudanca", [
        {"payload": "texto"}, {"device_id": 7}, {"fonte": "x"}, {"user_id": None}, {"schema_version": 1.5},
        {"instante_ingestao": datetime(2026, 10, 21)},
    ], ids=repr)
    def test_resposta_malformada_levanta(self, mudanca):
        with pytest.raises(laco.LinhaMalformada):
            laco.resposta_de_linha({"id": 1, **resposta(0, "m1"), **mudanca})

    def test_a_mensagem_de_erro_nunca_traz_conteudo_da_linha(self):
        linha = {"id": 1, **leitura_app(0, 1), "payload": ["segredo-no-payload"]}
        with pytest.raises(laco.LinhaMalformada) as e:
            laco.leitura_de_linha(linha)
        assert "segredo" not in str(e.value)


# ============================================================================ ciclo de tres fluxos
class TestProcessarCiclo:
    def test_os_tres_cursores_avancam_juntos(self, mundo):
        m = mundo
        m.borda.gravar_leituras([leitura_app(0, 1), leitura_app(1, 2)])
        m.borda.gravar_resposta(resposta(1, "desconhecida"))
        m.borda.gravar_entrega(entrega(1))
        res = ciclo_analise.processar_ciclo(m.analise, lambda tx, lote: {"x": lote.total}, versao_codigo="v-teste")
        assert (res.leituras, res.respostas, res.entregas, res.total) == (2, 1, 1, 4)
        c = m.analise.ler_cursor()
        assert (c["ultimo_leitura_id"], c["ultimo_resposta_id"], c["ultimo_entrega_id"]) == (2, 1, 1)
        assert c["ultimo_leitura_instante"] == T0 + timedelta(seconds=1)
        assert c["versao_codigo"] == "v-teste" and c["metricas"]["x"] == 4

    def test_excecao_desfaz_os_tres_cursores_e_a_releitura_pega_o_mesmo_lote(self, mundo):
        m = mundo
        m.borda.gravar_leituras([leitura_app(0, 1)])
        m.borda.gravar_resposta(resposta(0, "m1"))
        m.borda.gravar_entrega(entrega(0))

        def falha(tx, lote):
            tx.gravar_cursor(metricas={"escrito_antes_da_falha": 1})
            raise RuntimeError("falhou no meio")

        with pytest.raises(RuntimeError):
            ciclo_analise.processar_ciclo(m.analise, falha)
        c = m.analise.ler_cursor()
        assert (c["ultimo_leitura_id"], c["ultimo_resposta_id"], c["ultimo_entrega_id"]) == (0, 0, 0)
        assert "escrito_antes_da_falha" not in c["metricas"]
        assert ciclo_analise.processar_ciclo(m.analise, lambda tx, lote: None).total == 3

    def test_sem_dado_novo_so_renova_o_batimento(self, mundo):
        m = mundo
        antes = m.analise.ler_cursor()["atualizado_em"]
        m.rel.ir_para(5)
        res = ciclo_analise.processar_ciclo(m.analise, lambda tx, lote: pytest.fail("nao deveria processar"))
        assert res.total == 0 and not res.cheio
        assert m.analise.ler_cursor()["atualizado_em"] == T0 + timedelta(seconds=5) > antes

    def test_lacunas_dos_tres_fluxos_somam_em_lacunas_ignoradas(self, mundo):
        m = mundo
        m.borda.gravar_leituras([leitura_app(0, 1), leitura_app(1, 2), leitura_app(2, 3)])
        m.bd.tabelas["leituras"] = [l for l in m.bd.tabelas["leituras"] if l["id"] != 2]    # buraco no meio
        m.rel.ir_para(10)                                                                   # velho: permanente
        ciclo_analise.processar_ciclo(m.analise, lambda tx, lote: None)
        assert m.analise.ler_cursor()["metricas"]["lacunas_ignoradas"] == 1

    def test_lote_cheio_e_sinalizado(self, mundo):
        m = mundo
        m.borda.gravar_leituras([leitura_app(i, i + 1) for i in range(3)])
        assert ciclo_analise.processar_ciclo(m.analise, lambda tx, lote: None, limite=3).cheio is True
        m.borda.gravar_leituras([leitura_app(10, 20)])
        assert ciclo_analise.processar_ciclo(m.analise, lambda tx, lote: None, limite=3).cheio is False

    def test_somar_metricas(self):
        assert ciclo_analise.somar_metricas({"a": 1}, {"a": 2, "b": 3, "c": 0}) == {"a": 3, "b": 3}
        assert ciclo_analise.somar_metricas({"a": 1}, {"c": 0}) is None
        assert ciclo_analise.somar_metricas(None, None) is None


# ============================================================================ o laco
class TestLaco:
    def test_sem_banco_o_ciclo_nao_faz_nada(self):
        assert laco.LacoAnalise(obter_repo=lambda: None).executar_ciclo() is None

    def test_ciclo_vazio_renova_o_batimento_e_nao_escreve_saida(self, mundo):
        m = mundo
        m.rel.ir_para(3)
        res = m.laco.executar_ciclo()
        assert res.total == 0
        assert m.bd.cursor_analise["atualizado_em"] == T0 + timedelta(seconds=3)
        assert saidas_vazias(m) and not m.decisoes

    def test_cursores_metricas_e_versao_do_codigo(self, mundo):
        m = mundo
        m.borda.gravar_leituras([leitura_app(0, 1), leitura_app(1, 2)])
        m.borda.gravar_resposta(resposta(1, "desconhecida"))
        m.borda.gravar_entrega(entrega(1))
        m.borda.gravar_entrega(entrega(2, "sem_conexao"))
        m.rel.ir_para(3)
        m.laco.executar_ciclo()
        c = m.bd.cursor_analise
        assert (c["ultimo_leitura_id"], c["ultimo_resposta_id"], c["ultimo_entrega_id"]) == (2, 1, 2)
        assert c["versao_codigo"] == const_analise.VERSAO_CODIGO
        assert c["metricas"][laco.M_LEITURAS] == 2 and c["metricas"][laco.M_RESPOSTAS] == 1
        assert c["metricas"][laco.M_ENTREGAS] == 2
        assert c["metricas"][laco.PREFIXO_ENTREGA + "enviado"] == 1
        assert c["metricas"][laco.PREFIXO_ENTREGA + "sem_conexao"] == 1

    def test_segundo_ciclo_nao_reprocessa(self, mundo):
        m = mundo
        m.borda.gravar_leituras([leitura_app(0, 1)])
        m.rel.ir_para(1)
        m.laco.executar_ciclo()
        m.laco.executar_ciclo()
        assert metricas(m)[laco.M_LEITURAS] == 1

    def test_queda_vira_decisao_so_no_log_e_nenhuma_saida_e_gravada(self, mundo):
        m = mundo
        m.borda.gravar_leituras(queda_wokwi())
        m.rel.ir_para(4)
        m.laco.executar_ciclo()
        (cmd,) = comandos(m, mensagens.TIPO_SOLICITADA)
        assert cmd.episodio.estado == "aguardando_confirmacao" and cmd.criado_em == T0 + timedelta(seconds=4)
        assert [(o, r) for _d, o, r in m.decisoes if _d.comandos] == [("leitura", False)]
        assert metricas(m)[laco.M_COMANDOS] == 1
        assert saidas_vazias(m)                      # ensaio (agir=False): o laco NAO grava alertas, comandos, episodios...

    def test_o_log_mostra_a_decisao_sem_conteudo_da_leitura(self, mundo, capsys):
        m = mundo
        m.laco = laco.LacoAnalise(obter_repo=lambda: m.analise, relogio=m.rel)     # destino padrao: log
        m.borda.gravar_leituras(queda_wokwi())
        m.rel.ir_para(4)
        m.laco.executar_ciclo()
        linhas = [l for l in capsys.readouterr().out.splitlines() if "[laco]" in l]    # as regras tem os seus prints
        saida = "\n".join(linhas)
        assert f"comando={mensagens.TIPO_SOLICITADA}" in saida and "user=1" in saida
        assert "bridge-dev" not in saida and "idoso-sim" not in saida and "payload" not in saida

    def test_usa_estado_proprio_e_nao_toca_no_padrao(self, mundo):
        m = mundo
        m.borda.gravar_leituras(queda_wokwi())
        m.rel.ir_para(4)
        m.laco.executar_ciclo()
        assert m.laco.estado is not nucleo.PADRAO
        assert m.laco.estado.queda.episodios                    # o laco abriu o episodio...
        assert not nucleo.PADRAO.queda.episodios                # ...e o estado PADRAO segue limpo
        assert not nucleo.PADRAO.imobilidade.estados

    def test_resposta_estou_bem_encerra_o_episodio_do_laco(self, mundo):
        m = mundo
        m.borda.gravar_leituras(queda_wokwi())
        m.rel.ir_para(4)
        m.laco.executar_ciclo()
        (cmd,) = comandos(m, mensagens.TIPO_SOLICITADA)
        m.borda.gravar_resposta(resposta(6, cmd.mensagem["messageId"]))
        m.rel.ir_para(6)
        m.laco.executar_ciclo()
        (enc,) = comandos(m, mensagens.TIPO_ENCERRADA)
        assert enc.episodio.estado == "encerrado" and enc.episodio.motivo_encerramento == "CONFIRMADO"
        assert m.decisoes[-1][1] == "resposta"
        assert metricas(m)[laco.M_RESPOSTAS] == 1

    def test_resposta_de_outro_usuario_nao_encerra_nada_ds1(self, mundo):
        m = mundo
        m.borda.gravar_leituras(queda_wokwi())
        m.rel.ir_para(4)
        m.laco.executar_ciclo()
        (cmd,) = comandos(m, mensagens.TIPO_SOLICITADA)
        m.borda.gravar_resposta(resposta(5, cmd.mensagem["messageId"], user_id=2))
        m.rel.ir_para(5)
        m.laco.executar_ciclo()
        assert not comandos(m, mensagens.TIPO_ENCERRADA)

    def test_o_vigia_do_laco_expira_o_episodio_com_o_relogio_real_atrasado(self, mundo):
        m = mundo
        m.borda.gravar_leituras(queda_wokwi())
        m.rel.ir_para(4)
        m.laco.executar_ciclo()
        prazo = const_comum.TIMEOUT_CONFIRMACAO_S + const_comum.MARGEM_SERVIDOR_S
        # ainda dentro do prazo + atraso do vigia: nada expira
        m.rel.ir_para(4 + prazo + const_analise.ATRASO_VIGIA_S - 0.1)
        m.laco.executar_ciclo()
        assert not comandos(m, mensagens.TIPO_ENCERRADA)
        m.rel.ir_para(4 + prazo + const_analise.ATRASO_VIGIA_S + 0.1)
        m.laco.executar_ciclo()
        (enc,) = comandos(m, mensagens.TIPO_ENCERRADA)
        assert enc.episodio.motivo_encerramento == "EXPIRADO"
        assert m.decisoes[-1][1:] == ("vigia", False)
        assert saidas_vazias(m)

    def test_o_atraso_do_vigia_cobre_a_resposta_que_ainda_nao_foi_lida(self, mundo):
        """A resposta chega ao banco com instante ANTES do prazo, mas so aparece depois que o ciclo ja leu. Sem o atraso
        do vigia o episodio expiraria nesse ciclo; com ele, a resposta lida no ciclo seguinte ainda o CONFIRMA."""
        m = mundo
        m.borda.gravar_leituras(queda_wokwi())
        m.rel.ir_para(4)
        m.laco.executar_ciclo()
        (cmd,) = comandos(m, mensagens.TIPO_SOLICITADA)
        prazo = const_comum.TIMEOUT_CONFIRMACAO_S + const_comum.MARGEM_SERVIDOR_S
        m.rel.ir_para(4 + prazo + 0.2)                        # o relogio ja passou do prazo do episodio...
        m.laco.executar_ciclo()                               # ...mas a resposta ainda nao esta no banco
        assert not comandos(m, mensagens.TIPO_ENCERRADA)      # (atraso do vigia: relogio - ATRASO_VIGIA_S < prazo)
        m.borda.gravar_resposta(resposta(4 + prazo - 0.1, cmd.mensagem["messageId"]))
        m.rel.ir_para(4 + prazo + 0.3)
        m.laco.executar_ciclo()
        (enc,) = comandos(m, mensagens.TIPO_ENCERRADA)
        assert enc.episodio.motivo_encerramento == "CONFIRMADO"

    def test_linha_antiga_so_reconstroi_estado(self, mundo):
        m = mundo
        m.borda.gravar_leituras(queda_wokwi())
        m.rel.ir_para(4 + const_analise.IDADE_MAX_ACAO_S + 1)
        m.laco.executar_ciclo()
        decisoes_de_leitura = [(o, r) for d, o, r in m.decisoes if o == "leitura" and d.comandos]
        assert decisoes_de_leitura == [("leitura", True)]       # decidiu, mas marcada: o laco que age nao agira
        assert metricas(m)[laco.M_LEITURAS_ANTIGAS] == 5
        assert metricas(m)[laco.M_DECISOES_ANTIGAS] >= 1

    def test_linha_recente_nao_e_marcada_como_antiga(self, mundo):
        m = mundo
        m.borda.gravar_leituras(queda_wokwi())
        m.rel.ir_para(4)
        m.laco.executar_ciclo()
        assert laco.M_LEITURAS_ANTIGAS not in metricas(m) and laco.M_DECISOES_ANTIGAS not in metricas(m)

    # ---- lacuna de id ----
    def test_lacuna_recente_espera_e_nao_perde_leitura(self, mundo):
        m = mundo
        m.borda.gravar_leituras([leitura_app(i, i + 1) for i in range(4)])
        m.bd.tabelas["leituras"] = [l for l in m.bd.tabelas["leituras"] if l["id"] != 2]
        m.rel.ir_para(2.5)                       # a linha depois do buraco tem 0,5 s: buraco recente
        m.laco.executar_ciclo()
        assert m.bd.cursor_analise["ultimo_leitura_id"] == 1 and metricas(m).get(laco.M_LEITURAS) == 1
        # o id 2 aparece (commit fora de ordem): nada foi pulado
        m.bd.tabelas["leituras"].insert(1, {"id": 2, **leitura_app(1, 2)})
        m.laco.executar_ciclo()
        assert m.bd.cursor_analise["ultimo_leitura_id"] == 4 and metricas(m)[laco.M_LEITURAS] == 4

    def test_lacuna_antiga_e_permanente_o_cursor_nao_trava(self, mundo):
        m = mundo
        m.borda.gravar_leituras([leitura_app(i, i + 1) for i in range(4)])
        m.bd.tabelas["leituras"] = [l for l in m.bd.tabelas["leituras"] if l["id"] != 2]
        m.rel.ir_para(10)
        m.laco.executar_ciclo()
        assert m.bd.cursor_analise["ultimo_leitura_id"] == 4
        assert metricas(m)["lacunas_ignoradas"] == 1 and metricas(m)[laco.M_LEITURAS] == 3

    # ---- malformada e falha ----
    def test_leitura_malformada_e_contada_e_o_cursor_avanca(self, mundo, capsys):
        m = mundo
        ruim_payload = {**leitura_app(1, 2), "payload": ["lixo"]}
        ruim_device = {**leitura_app(2, 3), "device_id": None}
        ruim_fuso = {**leitura_app(3, 4), "instante_ingestao": datetime(2026, 10, 21, 12, 0, 3)}
        m.borda.gravar_leituras([leitura_app(0, 1), ruim_payload, ruim_device, ruim_fuso, leitura_app(4, 5)])
        m.rel.ir_para(5)
        m.laco.executar_ciclo()                  # nao levanta
        assert m.bd.cursor_analise["ultimo_leitura_id"] == 5
        assert metricas(m)[laco.M_LEITURAS_MALFORMADAS] == 3 and metricas(m)[laco.M_LEITURAS] == 2
        saida = capsys.readouterr().out
        assert "malformada id=2" in saida and "lixo" not in saida

    def test_resposta_malformada_e_contada(self, mundo):
        m = mundo
        m.borda.gravar_resposta({**resposta(0, "m1"), "payload": "nao-e-objeto"})
        m.borda.gravar_resposta(resposta(1, "m2"))
        m.rel.ir_para(2)
        m.laco.executar_ciclo()
        assert metricas(m)[laco.M_RESPOSTAS_MALFORMADAS] == 1 and metricas(m)[laco.M_RESPOSTAS] == 1
        assert m.bd.cursor_analise["ultimo_resposta_id"] == 2

    def test_falha_numa_regra_e_contada_e_nao_trava_o_cursor(self, mundo, monkeypatch):
        m = mundo
        original = nucleo.processar_leitura

        def quebra_no_usuario_99(estado, leitura):
            if leitura.user_id == 99:
                raise RuntimeError("bug na regra")
            return original(estado, leitura)

        monkeypatch.setattr(nucleo, "processar_leitura", quebra_no_usuario_99)
        m.borda.gravar_leituras([leitura_app(0, 1, user_id=99), leitura_app(1, 2, user_id=1)])
        m.rel.ir_para(2)
        m.laco.executar_ciclo()
        assert metricas(m)[laco.M_FALHAS] == 1 and metricas(m)[laco.M_LEITURAS] == 1
        assert m.bd.cursor_analise["ultimo_leitura_id"] == 2

    def test_falha_ao_registrar_a_decisao_nao_derruba_o_ciclo(self, mundo):
        m = mundo

        def quebra(*_a):
            raise RuntimeError("log quebrado")
        lc = laco.LacoAnalise(obter_repo=lambda: m.analise, relogio=m.rel, ao_decidir=quebra)
        m.borda.gravar_leituras(queda_wokwi())
        m.rel.ir_para(4)
        lc.executar_ciclo()
        assert m.bd.cursor_analise["ultimo_leitura_id"] == 5

    def test_falha_do_banco_nao_avanca_o_cursor(self, mundo, monkeypatch):
        m = mundo
        m.borda.gravar_leituras([leitura_app(0, 1)])
        m.rel.ir_para(1)

        def quebra(*_a, **_k):
            raise ConnectionError("banco fora do ar")
        monkeypatch.setattr(m.analise, "gravar_cursor", quebra)
        with pytest.raises(ConnectionError):
            m.laco.executar_ciclo()
        assert m.bd.cursor_analise["ultimo_leitura_id"] == 0
        monkeypatch.undo()
        m.laco.executar_ciclo()
        assert m.bd.cursor_analise["ultimo_leitura_id"] == 1

    def test_entrega_sem_conexao_aparece_no_log(self, mundo, capsys):
        m = mundo
        m.borda.gravar_entrega(entrega(0, "sem_conexao"))
        m.rel.ir_para(1)
        m.laco.executar_ciclo()
        assert "resultado=sem_conexao" in capsys.readouterr().out

    def test_ordena_leituras_e_respostas_pelo_instante_da_ingestao(self, mundo, monkeypatch):
        m = mundo
        visto = []
        monkeypatch.setattr(nucleo, "processar_leitura", lambda e, i: (visto.append(("L", i.instante)), nucleo.Decisoes())[1])
        monkeypatch.setattr(nucleo, "processar_resposta", lambda e, i: (visto.append(("R", i.instante)), nucleo.Decisoes())[1])
        m.borda.gravar_leituras([leitura_app(0, 1), leitura_app(4, 2)])
        m.borda.gravar_resposta(resposta(2, "m1"))
        m.rel.ir_para(5)
        m.laco.executar_ciclo()
        assert [t for t, _ in visto] == ["L", "R", "L"]


# ============================================================================ thread
def _esperar(condicao, prazo=3.0):
    fim = time.monotonic() + prazo
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(0.01)
    return condicao()


class TestThread:
    def test_inicia_processa_e_para(self, mundo, monkeypatch):
        m = mundo
        monkeypatch.setattr(const_analise, "INTERVALO_LEITURA_S", 0.01)
        m.borda.gravar_leituras([leitura_app(0, 1), leitura_app(1, 2)])
        m.rel.ir_para(2)
        t = m.laco.iniciar()
        try:
            assert t.name == "flow-laco-analise" and t.daemon
            assert _esperar(lambda: m.bd.cursor_analise["ultimo_leitura_id"] == 2)
            assert m.laco.iniciar() is t                    # chamar de novo nao sobe outra thread
        finally:
            m.laco.parar()
        assert not t.is_alive()

    def test_o_batimento_continua_sem_dado_novo(self, mundo, monkeypatch):
        m = mundo
        monkeypatch.setattr(const_analise, "INTERVALO_LEITURA_S", 0.01)
        m.laco.iniciar()
        try:
            m.rel.ir_para(7)
            assert _esperar(lambda: m.bd.cursor_analise["atualizado_em"] == T0 + timedelta(seconds=7))
        finally:
            m.laco.parar()

    def test_falha_do_banco_nao_mata_a_thread_e_o_log_nao_inunda(self, monkeypatch, capsys):
        monkeypatch.setattr(const_analise, "INTERVALO_LEITURA_S", 0.005)
        rel = Relogio()

        def sem_banco():
            raise ConnectionError("banco fora do ar")
        lc = laco.LacoAnalise(obter_repo=sem_banco, relogio=rel)
        t = lc.iniciar()
        try:
            assert _esperar(lambda: lc.falhas_seguidas >= 3)
            assert t.is_alive()
        finally:
            lc.parar()
        assert capsys.readouterr().out.count("falha no ciclo") == 1       # relogio parado: so a primeira linha

    def test_depois_de_parar_o_laco_nao_roda_mais(self, mundo, monkeypatch):
        m = mundo
        monkeypatch.setattr(const_analise, "INTERVALO_LEITURA_S", 0.01)
        t = m.laco.iniciar()
        m.laco.parar()
        assert not t.is_alive()
        m.borda.gravar_leituras([leitura_app(0, 1)])
        time.sleep(0.05)
        assert m.bd.cursor_analise["ultimo_leitura_id"] == 0
