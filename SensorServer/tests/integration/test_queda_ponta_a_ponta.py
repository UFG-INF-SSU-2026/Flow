"""
Queda de ponta a ponta com servidor real (equivale a simular_queda.py): cenarios (1) a (8) do plano.

O tempo da regra de queda e da mensagem e congelado pela fixture `relogio` e avancado pelo teste, entao nao ha
espera real de 3 s de repouso nem de 7 s de prazo. A thread vigia e as conexoes SSE sao reais.

  (1) pedido nos dois canais          (5) gatilho B e B ignorado com A aberto
  (2) ack + ESTOU_BEM = CONFIRMADO    (6) segundo episodio dentro do cooldown
  (3) sem resposta = EXPIRADO         (7) telemetria do app dispara o gatilho A
  (4) resposta tardia                 (8) conteudo do confirmacoes_usuario_<id>.jsonl
"""
import glob
import os

import pytest

from comum import const as const_comum
from analise import const as const_analise
from comum import mensagens
from analise import regra_queda
from _apoio import aguardar
from _cenarios import FluxoQueda

TIPOS_CICLO_CONFIRMADO = ["episodio.aberto", "push.ack", "confirmacao.resposta", "episodio.encerrado"]


def _entregas(banco) -> list[tuple]:
    """(canal, resultado) de cada linha de `entregas_push`: a entrega agora e da borda e fica no banco."""
    return [(e["canal"], e["resultado"]) for e in banco.tabelas["entregas_push"]]


class Cenario(FluxoQueda):
    """FluxoQueda com idoso1 e bridge1 conectados nos dois canais SSE."""

    def __init__(self, srv, relogio):
        super().__init__(srv, relogio)
        self.sse_app = srv.sse("/push/app", self.tok_app)
        self.sse_wk = srv.sse("/push/wokwi", self.tok_wk)
        for s in (self.sse_app, self.sse_wk):
            s.esperar_comentario("conectado")

    def fechar(self):
        self.sse_app.fechar()
        self.sse_wk.fechar()

    def pushes(self):
        """Proximo push de cada canal: (no app, no wokwi)."""
        return self.sse_app.proximo_push(), self.sse_wk.proximo_push()

    def provocar_queda(self):
        """Queda pelo gatilho A. Devolve o pedido recebido em cada canal: (no app, no wokwi)."""
        self.enviar_queda()
        return self.pushes()

    def sem_push_nos_dois(self, janela: float = 0.4) -> bool:
        return self.sse_app.sem_push(janela) and self.sse_wk.sem_push(janela)


@pytest.fixture
def cenario(relogio, servidor_real):
    c = Cenario(servidor_real, relogio)
    yield c
    c.fechar()


def _abertos(ler_confirmacoes) -> list[dict]:
    return [l for l in ler_confirmacoes(1) if l["tipo"] == "episodio.aberto"]


# ------------------------------------------------------------------ (1) a (3)
def test_1_pedido_chega_nos_dois_canais(cenario, ler_confirmacoes, banco):
    msg_app, msg_wk = cenario.provocar_queda()

    assert msg_app["messageId"] == msg_wk["messageId"]
    assert msg_app["episodeId"] == msg_wk["episodeId"]
    for m in (msg_app, msg_wk):
        assert m["messageType"] == mensagens.TIPO_SOLICITADA
        assert m["userId"] == 1
        assert m["payload"]["timeoutMs"] == 5000        # tempo congelado: sem desconto de replay
        assert m["payload"]["motivo"] == const_analise.MOTIVO_A

    assert aguardar(lambda: len(banco.tabelas["entregas_push"]) >= 2)
    assert set(_entregas(banco)) == {("app", "enviado"), ("wokwi", "enviado")}


def test_2_ack_e_estou_bem_encerram_como_confirmado(cenario, ler_confirmacoes):
    pedido, _ = cenario.provocar_queda()
    mid = pedido["messageId"]

    cenario.ack_bridge(mid)
    cenario.resposta_app(mid)

    enc_app, enc_wk = cenario.pushes()
    for enc in (enc_app, enc_wk):
        assert enc["messageType"] == mensagens.TIPO_ENCERRADA
        assert enc["payload"]["resultado"] == "CONFIRMADO"
        assert enc["episodeId"] == pedido["episodeId"]
        assert enc["messageId"] != mid
    assert regra_queda._episodios[1].resultado == "CONFIRMADO"
    assert [l["tipo"] for l in ler_confirmacoes(1)] == TIPOS_CICLO_CONFIRMADO


def test_3_sem_resposta_expira_depois_de_timeout_mais_margem(cenario, ler_confirmacoes, banco):
    pedido, _ = cenario.provocar_queda()

    cenario.relogio.avancar(const_comum.TIMEOUT_CONFIRMACAO_S)        # passou o tempo do idoso, falta a margem
    assert cenario.sem_push_nos_dois()
    assert regra_queda._episodios[1].encerrado_em is None

    cenario.relogio.avancar(const_comum.MARGEM_SERVIDOR_S)            # agora vence o prazo do servidor
    enc_app, enc_wk = cenario.pushes()
    for enc in (enc_app, enc_wk):
        assert enc["messageType"] == mensagens.TIPO_ENCERRADA
        assert enc["payload"]["resultado"] == "EXPIRADO"
        assert enc["episodeId"] == pedido["episodeId"]
    assert regra_queda._episodios[1].resultado == "EXPIRADO"

    # a ultima linha de entrega e gravada logo depois do envio; espera ela aparecer
    assert aguardar(lambda: len(banco.tabelas["entregas_push"]) >= 4)
    assert aguardar(lambda: len(ler_confirmacoes(1)) >= 2)
    assert [l["tipo"] for l in ler_confirmacoes(1)] == ["episodio.aberto", "episodio.encerrado"]


# ------------------------------------------------------------------------ (4)
def test_4_resposta_tardia_depois_do_encerramento_e_marcada_como_tardia(cenario, ler_confirmacoes):
    pedido, _ = cenario.provocar_queda()
    mid = pedido["messageId"]
    cenario.relogio.avancar(const_comum.TIMEOUT_CONFIRMACAO_S + const_comum.MARGEM_SERVIDOR_S)
    for enc in cenario.pushes():
        assert enc["payload"]["resultado"] == "EXPIRADO"
    assert aguardar(lambda: len(ler_confirmacoes(1)) >= 2)       # o encerramento ja foi gravado
    antes = len(ler_confirmacoes(1))

    cenario.resposta_app(mid)                                    # ESTOU_BEM que chegou atrasado
    cenario.ack_bridge(mid)                                      # e o ack do bridge, tambem atrasado

    novas = ler_confirmacoes(1)[antes:]
    assert [l["tipo"] for l in novas] == ["confirmacao.resposta", "push.ack"]
    for linha in novas:
        assert linha["tardia"] is True
        assert linha["episodeId"] == pedido["episodeId"]         # a mensagem e conhecida: so chegou tarde
        assert "mensagem_desconhecida" not in linha
    assert regra_queda._episodios[1].resultado == "EXPIRADO"     # a resposta tardia nao muda o desfecho
    assert cenario.sem_push_nos_dois()                           # e nao gera novo encerramento


# ------------------------------------------------------------------------ (5)
def test_5_gatilho_b_abre_episodio_por_imobilidade_prolongada(cenario, ler_confirmacoes):
    cenario.decisao_wokwi("ALERTA_IMOBILIDADE")

    msg_app, msg_wk = cenario.pushes()
    assert msg_app["messageId"] == msg_wk["messageId"]
    for m in (msg_app, msg_wk):
        assert m["messageType"] == mensagens.TIPO_SOLICITADA
        assert m["payload"]["motivo"] == const_analise.MOTIVO_B
        assert m["payload"]["timeoutMs"] == 5000
    (aberto,) = _abertos(ler_confirmacoes)
    assert aberto["motivo"] == const_analise.MOTIVO_B and aberto["episodeId"] == msg_app["episodeId"]
    assert "fonte_do_disparo" not in aberto                      # so o gatilho A traz os dados da fusao
    # o fluxo legado de imobilidade continua gerando o seu alerta
    assert glob.glob(os.path.join(const_comum.DATA_DIR, "alerta_imobilidade_*.json"))


def test_5_gatilho_b_e_ignorado_com_episodio_do_gatilho_a_aberto(cenario, ler_confirmacoes):
    pedido, _ = cenario.provocar_queda()

    cenario.decisao_wokwi("ALERTA_IMOBILIDADE")

    assert cenario.sem_push_nos_dois()                           # nenhum segundo pedido
    ep = regra_queda._episodios[1]
    assert ep.episode_id == pedido["episodeId"] and ep.motivo == const_analise.MOTIVO_A and ep.encerrado_em is None
    (aberto,) = _abertos(ler_confirmacoes)
    assert aberto["motivo"] == const_analise.MOTIVO_A


# ------------------------------------------------------------------------ (6)
def test_6_segundo_episodio_dentro_do_cooldown_nao_abre(cenario, ler_confirmacoes):
    pedido, _ = cenario.provocar_queda()
    cenario.resposta_app(pedido["messageId"])                    # encerra como CONFIRMADO
    cenario.pushes()                                             # consome o encerramento

    cenario.enviar_queda()                                       # novo impacto + repouso, poucos segundos depois

    assert cenario.sem_push_nos_dois()
    assert len(_abertos(ler_confirmacoes)) == 1
    assert regra_queda._episodios[1].episode_id == pedido["episodeId"]

    # controle: passado o cooldown, a mesma sequencia abre outro episodio
    cenario.relogio.avancar(const_analise.COOLDOWN_S)
    cenario.enviar_queda()
    novo_app, novo_wk = cenario.pushes()
    assert novo_app["episodeId"] == novo_wk["episodeId"] != pedido["episodeId"]
    assert len(_abertos(ler_confirmacoes)) == 2


# ------------------------------------------------------------------------ (7)
def test_7_telemetria_do_app_dispara_o_gatilho_a_com_limiares_do_app(cenario, ler_confirmacoes):
    # dinamica de ~16,2 m/s2: acima do impacto do app (15) e abaixo do impacto do wokwi (20)
    assert const_analise.LIMIAR_IMPACTO["app"] < 16.0 < const_analise.LIMIAR_IMPACTO["wokwi"]
    cenario.leitura_app(9.81, 10.0)                              # em repouso
    cenario.relogio.avancar(1)
    cenario.leitura_app(20.0, 26.0)                              # impacto
    for _ in range(int(const_analise.T_POS_IMPACTO_S)):
        cenario.relogio.avancar(1)
        cenario.leitura_app(9.81, 10.0)                          # repouso

    msg_app, msg_wk = cenario.pushes()
    assert msg_app["messageId"] == msg_wk["messageId"]
    assert msg_app["payload"]["motivo"] == const_analise.MOTIVO_A
    (aberto,) = _abertos(ler_confirmacoes)
    assert aberto["fonte_do_disparo"] == "app"
    assert [(f["fonte"], f["device_id"]) for f in aberto["fontes_usadas"]] == [("app", "cel1")]
    assert aberto["fontes_desatualizadas"] == []


def test_7_a_mesma_dinamica_vinda_do_wokwi_nao_dispara(cenario):
    cenario.leitura_wokwi(1.0, 1.5)
    cenario.relogio.avancar(1)
    cenario.leitura_wokwi(3.0, 16.2)                             # acima do impacto do app, abaixo do do wokwi
    for _ in range(int(const_analise.T_POS_IMPACTO_S)):
        cenario.relogio.avancar(1)
        cenario.leitura_wokwi(0.3, 0.4)

    assert cenario.sem_push_nos_dois()
    assert regra_queda._episodios == {}


# ------------------------------------------------------------------------ (8)
def test_8_conteudo_e_ordem_do_confirmacoes_jsonl(cenario, ler_confirmacoes, banco):
    pedido, _ = cenario.provocar_queda()
    mid, ep = pedido["messageId"], pedido["episodeId"]
    cenario.ack_bridge(mid)
    cenario.resposta_app(mid)
    enc_app, _ = cenario.pushes()

    linhas = ler_confirmacoes(1)
    assert [l["tipo"] for l in linhas] == TIPOS_CICLO_CONFIRMADO
    carimbos = [l["timestamp"] for l in linhas]
    assert all(carimbos) and carimbos == sorted(carimbos)        # ISO 8601 UTC: ordem textual = ordem no tempo

    aberto, ack, resp, encerrado = linhas
    assert aberto["episodeId"] == ep and aberto["motivo"] == const_analise.MOTIVO_A
    assert (ack["eventType"], ack["state"], ack["source"], ack["device_id"]) == (
        "push.ack", "ATIVADO", "wokwi", "bridge-dev")
    assert (resp["eventType"], resp["state"], resp["source"], resp["device_id"]) == (
        "confirmacao.resposta", "ESTOU_BEM", "app", "cel1")
    for retorno in (ack, resp):
        assert retorno["messageId"] == mid and retorno["episodeId"] == ep
        assert "tardia" not in retorno and "mensagem_desconhecida" not in retorno
    assert encerrado["episodeId"] == ep and encerrado["resultado"] == "CONFIRMADO"

    # as entregas (pedido e encerramento, canal por canal) ficam em `entregas_push`, uma linha por canal
    assert aguardar(lambda: len(banco.tabelas["entregas_push"]) >= 4)
    por_mensagem = {}
    for e in banco.tabelas["entregas_push"]:
        por_mensagem.setdefault(e["message_id"], set()).add((e["canal"], e["resultado"]))
    assert por_mensagem == {mid: {("app", "enviado"), ("wokwi", "enviado")},
                            enc_app["messageId"]: {("app", "enviado"), ("wokwi", "enviado")}}
