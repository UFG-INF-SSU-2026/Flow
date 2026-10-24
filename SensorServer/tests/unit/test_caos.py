"""
Testes de caos: cada um termina no comportamento esperado sob falha (docs/arquitetura.md, secao 12). Banco falso, relogio
explicito, ciclos sincronos: nenhum depende de Docker, thread ou espera.

  analise derrubada no meio de um episodio    TestAnaliseDerrubada
  borda derrubada durante a entrega           TestBordaDerrubada
  banco fora do ar                            TestBancoForaDoAr
  leitura malformada                          TestLeituraMalformada
  atraso grande seguido de recuperacao        TestAtrasoGrande
"""
from datetime import timedelta

import pytest

from _mundo import Mundo, T0, queda_wokwi, resposta
from analise import laco
from borda import const as const_borda
from borda import entrega_push, saude, server, tempo
from comum import mensagens
from flow_db import repositorios


class CanalFalso:
    CANAL = "app"

    def __init__(self):
        self.recebidas = []

    def enviar(self, user_id, msg):
        self.recebidas.append(msg)
        return "ENTREGUE_AO_CANAL"


class Morte(BaseException):
    """O processo morreu (nao e `Exception`: nada no codigo da borda deve engoli-la)."""


@pytest.fixture
def m():
    return Mundo()


def borda_de(m, canal):
    return entrega_push.EntregaPush(obter_repo=lambda: m.borda, relogio=m.rel, canais=[canal])


class TestAnaliseDerrubada:
    def test_derrubada_no_meio_do_episodio_o_push_sai_uma_vez_e_o_encerramento_chega(self, m):
        canal = CanalFalso()
        entrega = borda_de(m, canal)
        pedido = m.pedido_na_analise()
        entrega.executar_ciclo()
        m.reiniciar_analise()                                         # kill -9 e subida
        m.laco.executar_ciclo()                                       # nada novo: nao reabre nem reenvia
        m.borda.gravar_resposta(resposta(6, pedido["message_id"]))
        m.rel.ir_para(7)
        m.laco.executar_ciclo()
        entrega.executar_ciclo()
        assert [x["messageType"] for x in canal.recebidas] == [mensagens.TIPO_SOLICITADA, mensagens.TIPO_ENCERRADA]
        assert {x["episodeId"] for x in canal.recebidas} == {pedido["episodio_id"]}
        assert len(m.t("episodios")) == 1

    def test_derrubada_no_meio_do_ciclo_o_mesmo_lote_e_refeito_sem_duplicar(self, m, monkeypatch):
        m.borda.gravar_leituras(queda_wokwi())
        m.rel.ir_para(4)
        original = m.analise.gravar_comando

        def morre(c):
            raise Morte()
        monkeypatch.setattr(m.analise, "gravar_comando", morre)
        with pytest.raises(Morte):
            m.laco.executar_ciclo()
        assert m.bd.cursor_analise["ultimo_leitura_id"] == 0 and not m.contagens()["comandos_push"]
        monkeypatch.setattr(m.analise, "gravar_comando", original)
        m.reiniciar_analise()
        m.laco.executar_ciclo()
        assert len(m.comandos(mensagens.TIPO_SOLICITADA)) == 1 and len(m.t("episodios")) == 1


class TestBordaDerrubada:
    def comando(self, m, i, emitido_em, criado_em):
        msg = mensagens.criar_pedido_confirmacao(1, f"ep-{i}", 5, "IMPACTO_SEGUIDO_DE_IMOBILIDADE", agora=emitido_em)
        m.analise.gravar_comando({"criado_em": criado_em, "user_id": 1, "device_id": None, "canal": "todos",
                                  "message_id": msg["messageId"], "tipo": msg["messageType"], "payload": msg,
                                  "expira_em": None, "episodio_id": f"ep-{i}"})
        return msg["messageId"]

    def test_borda_morre_entre_entregar_e_gravar_o_cursor_e_o_comando_sai_de_novo(self, m):
        m.rel.ir_para(10)
        ids = [self.comando(m, i, m.rel(), m.rel()) for i in (1, 2)]

        class MorreNoSegundo(CanalFalso):
            def enviar(self, user_id, msg):
                if self.recebidas:
                    raise Morte()
                return super().enviar(user_id, msg)
        canal = MorreNoSegundo()
        with pytest.raises(Morte):
            borda_de(m, canal).executar_ciclo()
        assert m.bd.cursor_borda["ultimo_comando_id"] == 0            # o cursor nao avancou
        depois = CanalFalso()
        assert borda_de(m, depois).executar_ciclo() == 2
        assert [x["messageId"] for x in depois.recebidas] == ids      # ao menos uma vez, mesmo message_id (o dispositivo ignora a duplicata)
        assert m.bd.cursor_borda["ultimo_comando_id"] == 2

    def test_comando_expirado_nao_e_entregue_e_a_analise_ve_o_resultado(self, m):
        self.comando(m, 1, T0, T0)
        m.rel.ir_para(const_borda.JANELA_COMANDOS_S - 1)              # ainda lido (dentro da janela), mas a mensagem ja venceu
        canal = CanalFalso()
        assert borda_de(m, canal).executar_ciclo() == 1
        assert canal.recebidas == []
        (entrega,) = m.t("entregas_push")
        assert entrega["resultado"] == "expirado"

    def test_comando_de_antes_da_parada_nem_e_lido(self, m):
        self.comando(m, 1, T0, T0)
        m.rel.ir_para(const_borda.JANELA_COMANDOS_S + 30)
        canal = CanalFalso()
        assert borda_de(m, canal).executar_ciclo() == 0 and canal.recebidas == []

    def test_a_analise_nao_e_afetada_pela_borda_parada(self, m):
        pedido = m.pedido_na_analise()                                # ninguem entrega: a borda esta parada
        m.rel.ir_para(4 + 20)
        m.laco.executar_ciclo()
        (enc,) = m.comandos(mensagens.TIPO_ENCERRADA)
        assert enc["episodio_id"] == pedido["episodio_id"]


class TestBancoForaDoAr:
    def test_analise_nao_avanca_o_cursor_nem_age_e_depois_recupera(self, m, monkeypatch):
        m.borda.gravar_leituras(queda_wokwi())
        m.rel.ir_para(4)
        original, estado = m.analise.ler_cursor, {"fora": True}

        def ler_cursor():
            if estado["fora"]:
                raise ConnectionError("banco fora")
            return original()
        monkeypatch.setattr(m.analise, "ler_cursor", ler_cursor)
        with pytest.raises(ConnectionError):
            m.laco.executar_ciclo()
        assert not m.comandos() and m.bd.cursor_analise["ultimo_leitura_id"] == 0
        estado["fora"] = False
        m.laco.executar_ciclo()
        assert len(m.comandos(mensagens.TIPO_SOLICITADA)) == 1 and m.bd.cursor_analise["ultimo_leitura_id"] == 5

    def test_borda_guarda_na_fila_descarta_o_mais_antigo_e_grava_quando_o_banco_volta(self, banco, monkeypatch, capsys):
        monkeypatch.setattr(const_borda, "MAX_FILA_LEITURAS", 3)
        original, estado = repositorios.borda().gravar_leituras, {"fora": True}

        def grava(lote):
            if estado["fora"]:
                raise ConnectionError("banco fora")
            return original(lote)
        monkeypatch.setattr(repositorios.borda(), "gravar_leituras", grava)
        for i in range(5):
            server._enfileirar_leitura(1, "app", "cel1", {"x": i}, 3, "idoso1", tempo.agora_utc())
        assert len(server._fila_leituras) == 3 and server._descartes_leituras == 2
        assert "descartada" in capsys.readouterr().out
        assert server._descarregar_leituras() is False                # banco fora: o lote volta para a fila
        assert [l["payload"]["x"] for l in server._fila_leituras] == [2, 3, 4]
        estado["fora"] = False
        repositorios._borda.disjuntor.sucesso()
        assert server._descarregar_leituras() is True
        assert [l["payload"]["x"] for l in banco.tabelas["leituras"]] == [2, 3, 4]

    def test_token_valido_continua_sendo_aceito_com_o_banco_fora(self, cli, tokens, banco, monkeypatch):
        def quebra(lote):
            raise ConnectionError("banco fora")
        monkeypatch.setattr(repositorios.borda(), "gravar_leituras", quebra)
        r = cli.post("/dados", {"schemaVersion": 3, "source": "app", "user_id": 1, "device_id": "cel1",
                                "timestamp_envio": 1_760_000_000_000,
                                "acelerometro": {"magnitude": 9.8, "magnitude_max_janela": 9.9}}, tokens["idoso"])
        assert r.status_code == 200
        assert server._descarregar_leituras() is False and len(server._fila_leituras) == 1

    def test_health_diz_que_nao_sabe_em_vez_de_dizer_que_esta_tudo_bem(self, banco, monkeypatch):
        def quebra():
            raise ConnectionError("banco fora")
        monkeypatch.setattr(repositorios.borda(), "ler_saude", quebra)
        monitor = saude.MonitorSaude()
        assert monitor.pipeline(forcar=True)["estado"] == saude.DESCONHECIDO


class TestLeituraMalformada:
    def test_nao_derruba_o_laco_o_cursor_avanca_e_a_queda_segue_detectada(self, m):
        linhas = queda_wokwi()
        ruim = queda_wokwi()[2]
        ruim["instante_ingestao"] += timedelta(milliseconds=500)
        ruim["payload"] = ["nao", "e", "objeto"]
        m.borda.gravar_leituras(linhas[:3] + [ruim] + linhas[3:])
        m.rel.ir_para(4)
        m.laco.executar_ciclo()
        assert m.bd.cursor_analise["ultimo_leitura_id"] == 6
        assert m.bd.cursor_analise["metricas"][laco.M_LEITURAS_MALFORMADAS] == 1
        assert len(m.comandos(mensagens.TIPO_SOLICITADA)) == 1


class TestAtrasoGrande:
    def test_analise_atrasada_acusa_descarta_por_idade_recupera_e_detecta_a_queda_nova(self, m, capsys):
        m.borda.gravar_leituras(queda_wokwi(t0=0, seq0=1))            # a analise esta parada: ninguem decide
        m.rel.ir_para(100)
        antes = saude.classificar(m.borda.ler_saude())
        assert antes["estado"] == saude.DEGRADADO and saude.MOTIVO_ANALISE_PARADA in antes["motivos"]

        m.reiniciar_analise()
        m.laco.executar_ciclo()                                       # processa o atraso: so reconstroi, nao age
        assert not any(m.contagens().values())
        assert m.bd.cursor_analise["metricas"][laco.M_LEITURAS_ANTIGAS] == 5
        saida = capsys.readouterr().out
        assert "ALARME descartadas por idade" in saida and "ALARME atraso" in saida
        assert saude.classificar(m.borda.ler_saude())["estado"] == saude.OK

        m.borda.gravar_leituras(queda_wokwi(t0=101, seq0=6))          # uma queda de agora
        m.rel.ir_para(105)
        m.laco.executar_ciclo()
        assert len(m.comandos(mensagens.TIPO_SOLICITADA)) == 1

    def test_alarmes_do_laco_sao_limitados_e_acumulam(self, m, capsys, monkeypatch):
        m.borda.gravar_leituras(queda_wokwi(t0=0, seq0=1))
        m.rel.ir_para(100)
        m.laco.executar_ciclo()
        capsys.readouterr()
        m.borda.gravar_leituras(queda_wokwi(t0=0, seq0=6))            # mais leituras velhas, 1 s depois do alarme
        m.rel.ir_para(101)
        m.laco.executar_ciclo()
        assert "ALARME" not in capsys.readouterr().out               # dentro do intervalo de log
        m.borda.gravar_leituras(queda_wokwi(t0=0, seq0=11))
        m.rel.ir_para(120)
        m.laco.executar_ciclo()
        assert "ALARME descartadas por idade: 10 leitura(s)" in capsys.readouterr().out    # as 10 acumuladas
