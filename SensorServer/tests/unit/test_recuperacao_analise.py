"""
Recuperacao da analise na subida (`LacoAnalise.recuperar_estado`) e as regras de linha antiga. Banco falso, relogio
explicito, ciclos sincronos. Cada teste "derruba" a analise com `Mundo.reiniciar_analise()`: o estado em memoria some, o banco fica.
"""
import time
from datetime import timedelta

import pytest

from _mundo import Mundo, T0, queda_wokwi, resposta, leitura_wokwi
from analise import const as const_analise
from analise import laco
from comum import const as const_comum
from comum import mensagens

PRAZO = const_comum.TIMEOUT_CONFIRMACAO_S + const_comum.MARGEM_SERVIDOR_S


@pytest.fixture
def m():
    return Mundo()


class TestRecuperarEstado:
    def test_sem_banco_devolve_none(self):
        lc = laco.LacoAnalise(obter_repo=lambda: None, agir=True)
        assert lc.recuperar_estado() is None and not lc.recuperado

    def test_banco_vazio_nao_tem_o_que_recuperar(self, m):
        assert m.laco.recuperar_estado() == laco.ResultadoRecuperacao(0, 0, 0, 0)
        assert m.laco.recuperado

    def test_impacto_em_andamento_continua_depois_do_reinicio(self, m):
        m.borda.gravar_leituras(queda_wokwi()[:3])                 # normal, impacto, primeiro repouso
        m.rel.ir_para(2)
        m.laco.executar_ciclo()
        assert not m.comandos()
        m.reiniciar_analise()
        m.borda.gravar_leituras(queda_wokwi(seq0=1)[3:])           # os repousos que faltam (t = 3 e 4)
        m.rel.ir_para(4)
        m.laco.executar_ciclo()
        assert len(m.comandos(mensagens.TIPO_SOLICITADA)) == 1

    def test_sem_recuperar_o_impacto_em_andamento_se_perde(self, m):
        m.borda.gravar_leituras(queda_wokwi()[:3])
        m.rel.ir_para(2)
        m.laco.executar_ciclo()
        m.reiniciar_analise(recuperar=False)
        m.borda.gravar_leituras(queda_wokwi()[3:])
        m.rel.ir_para(4)
        m.laco.executar_ciclo()
        assert not m.comandos()                                    # e por isso a recuperacao existe

    def test_episodio_aberto_volta_com_as_mesmas_mensagens(self, m):
        pedido = m.pedido_na_analise()
        resultado = m.reiniciar_analise()
        assert (resultado.episodios, resultado.abertos) == (1, 1)
        ep = m.laco.estado.queda.episodios[1]
        assert ep.episode_id == pedido["episodio_id"] and ep.encerrado_em is None
        assert m.laco.estado.queda.mensagens[pedido["message_id"]].episode_id == pedido["episodio_id"]

    def test_a_recuperacao_nao_grava_nada_nem_mexe_no_cursor(self, m):
        m.pedido_na_analise()
        antes = (m.contagens(), dict(m.bd.cursor_analise), len(m.t("entregas_push")))
        m.reiniciar_analise()
        assert (m.contagens(), dict(m.bd.cursor_analise), len(m.t("entregas_push"))) == antes

    def test_estou_bem_depois_do_reinicio_encerra_o_mesmo_episodio_sem_abrir_outro(self, m):
        pedido = m.pedido_na_analise()
        m.reiniciar_analise()
        m.borda.gravar_resposta(resposta(6, pedido["message_id"]))
        m.rel.ir_para(7)
        m.laco.executar_ciclo()
        (enc,) = m.comandos(mensagens.TIPO_ENCERRADA)
        assert enc["payload"]["payload"]["resultado"] == "CONFIRMADO" and enc["episodio_id"] == pedido["episodio_id"]
        assert len(m.comandos(mensagens.TIPO_SOLICITADA)) == 1 and len(m.t("episodios")) == 1
        assert m.t("episodios")[0]["estado"] == "encerrado"

    def test_sem_recuperar_o_episodio_aberto_nunca_encerra(self, m):
        pedido = m.pedido_na_analise()
        m.reiniciar_analise(recuperar=False)
        m.borda.gravar_resposta(resposta(6, pedido["message_id"]))
        m.rel.ir_para(4 + PRAZO + 20)
        m.laco.executar_ciclo()
        assert not m.comandos(mensagens.TIPO_ENCERRADA)
        assert m.t("episodios")[0]["estado"] == "aguardando_confirmacao"      # preso no banco: o defeito que a recuperacao corrige

    def test_episodio_que_expirou_durante_a_parada_e_encerrado_no_primeiro_ciclo(self, m):
        pedido = m.pedido_na_analise()
        m.rel.ir_para(4 + PRAZO + 20)
        m.reiniciar_analise()
        m.laco.executar_ciclo()
        (enc,) = m.comandos(mensagens.TIPO_ENCERRADA)
        assert enc["payload"]["payload"]["resultado"] == "EXPIRADO" and enc["episodio_id"] == pedido["episodio_id"]
        assert m.t("episodios")[0]["motivo_encerramento"] == "EXPIRADO"

    def test_resposta_antiga_que_encerra_episodio_real_e_aplicada(self, m):
        """O idoso respondeu enquanto a analise estava parada: a resposta ja e 'antiga' quando e lida, mas encerra um
        episodio que o banco tem. Sem aplica-la o episodio ficaria aberto para sempre."""
        pedido = m.pedido_na_analise()
        m.borda.gravar_resposta(resposta(6, pedido["message_id"]))
        m.rel.ir_para(100)
        m.reiniciar_analise()
        m.laco.executar_ciclo()
        (enc,) = m.comandos(mensagens.TIPO_ENCERRADA)
        assert enc["payload"]["payload"]["resultado"] == "CONFIRMADO"
        assert m.t("episodios")[0]["estado"] == "encerrado"
        assert len(m.comandos(mensagens.TIPO_ENCERRADA)) == 1             # o vigia nao o encerra de novo

    def test_cooldown_do_episodio_encerrado_sobrevive_ao_reinicio(self, m):
        pedido = m.pedido_na_analise()
        m.borda.gravar_resposta(resposta(6, pedido["message_id"]))
        m.rel.ir_para(7)
        m.laco.executar_ciclo()
        m.reiniciar_analise()
        m.borda.gravar_leituras(queda_wokwi(t0=10, seq0=6))        # outra queda, 6 s depois do encerramento
        m.rel.ir_para(14)
        m.laco.executar_ciclo()
        assert len(m.comandos(mensagens.TIPO_SOLICITADA)) == 1     # cooldown: nao abriu outro episodio

    def test_resposta_repetida_a_episodio_encerrado_e_tardia(self, m):
        pedido = m.pedido_na_analise()
        m.borda.gravar_resposta(resposta(6, pedido["message_id"]))
        m.rel.ir_para(7)
        m.laco.executar_ciclo()
        m.reiniciar_analise()
        m.borda.gravar_resposta(resposta(8, pedido["message_id"]))
        m.rel.ir_para(9)
        m.laco.executar_ciclo()
        assert len(m.comandos(mensagens.TIPO_ENCERRADA)) == 1
        assert any(c["dados"].get("tardia") is True for c in m.t("confirmacoes"))

    def test_janela_de_reprocessamento_deixa_de_fora_o_que_e_mais_antigo(self, m):
        m.borda.gravar_leituras([leitura_wokwi(0, 1, 3.0, 25.0), leitura_wokwi(100, 2, 0.3, 0.4)])
        m.rel.ir_para(100)
        m.laco.executar_ciclo()
        assert m.reiniciar_analise().leituras_relidas == 1

    def test_limite_de_reprocessamento_fica_com_as_mais_recentes(self, m, monkeypatch):
        m.borda.gravar_leituras(queda_wokwi())
        m.rel.ir_para(4)
        m.laco.executar_ciclo()
        monkeypatch.setattr(const_analise, "LIMITE_REPROCESSAMENTO", 2)
        assert m.reiniciar_analise().leituras_relidas == 2

    def test_leitura_malformada_na_janela_e_ignorada(self, m):
        ruim = leitura_wokwi(1, 2, 0.3, 0.4)
        ruim["payload"] = ["nao", "e", "objeto"]
        m.borda.gravar_leituras([leitura_wokwi(0, 1, 1.0, 1.5), ruim])
        m.rel.ir_para(2)
        m.laco.executar_ciclo()
        resultado = m.reiniciar_analise()
        assert (resultado.leituras_relidas, resultado.leituras_ignoradas) == (1, 1)

    def test_falha_do_banco_na_recuperacao_deixa_o_estado_como_estava(self, m, monkeypatch):
        lc = m.novo_laco()
        antes = lc.estado

        def quebra(*a, **k):
            raise ConnectionError("banco fora")
        monkeypatch.setattr(m.analise, "listar_episodios_recentes", quebra)
        with pytest.raises(ConnectionError):
            lc.recuperar_estado()
        assert lc.estado is antes and not lc.recuperado


class TestLinhaAntiga:
    def test_episodio_ficticio_de_linha_antiga_nao_bloqueia_a_queda_real(self, m):
        """Backlog com uma queda velha (sem acao) e uma queda de agora no mesmo ciclo: so a de agora vira pedido."""
        m.borda.gravar_leituras(queda_wokwi(t0=0, seq0=1) + queda_wokwi(t0=100, seq0=6))
        m.rel.ir_para(104)
        m.laco.executar_ciclo()
        (pedido,) = m.comandos(mensagens.TIPO_SOLICITADA)
        assert pedido["criado_em"] == T0 + timedelta(seconds=104)
        assert len(m.t("episodios")) == 1

    def test_episodio_ficticio_nao_poe_cooldown_depois_de_expirar(self, m):
        m.borda.gravar_leituras(queda_wokwi(t0=0, seq0=1))
        m.rel.ir_para(100)
        m.laco.executar_ciclo()                                    # so reconstrucao; o vigia roda no fim do ciclo
        assert not m.comandos() and 1 not in m.laco.estado.queda.episodios
        m.borda.gravar_leituras(queda_wokwi(t0=101, seq0=6))
        m.rel.ir_para(105)
        m.laco.executar_ciclo()
        assert len(m.comandos(mensagens.TIPO_SOLICITADA)) == 1


class TestThreadRecuperaAntesDeAgir:
    def test_sem_recuperar_nenhum_ciclo_roda_e_depois_ele_segue(self, m, monkeypatch):
        monkeypatch.setattr(const_analise, "INTERVALO_LEITURA_S", 0.01)
        m.borda.gravar_leituras(queda_wokwi())
        m.rel.ir_para(4)
        falhas = {"restam": 3, "chamadas": 0}
        original = m.analise.listar_episodios_recentes

        def instavel(*a, **k):
            falhas["chamadas"] += 1
            if falhas["restam"] > 0:
                falhas["restam"] -= 1
                raise ConnectionError("banco fora")
            return original(*a, **k)
        monkeypatch.setattr(m.analise, "listar_episodios_recentes", instavel)
        lc = m.laco = m.novo_laco(recuperar=True)
        lc.iniciar()
        try:
            assert _esperar(lambda: falhas["chamadas"] >= 2)
            if falhas["restam"] > 0:
                assert m.bd.cursor_analise["ultimo_leitura_id"] == 0 and not m.comandos()     # ninguem age sem recuperar
            assert _esperar(lambda: lc.recuperado and m.bd.cursor_analise["ultimo_leitura_id"] == 5)
        finally:
            lc.parar()
        assert len(m.comandos(mensagens.TIPO_SOLICITADA)) == 1


def _esperar(condicao, prazo=3.0):
    fim = time.monotonic() + prazo
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(0.01)
    return condicao()
