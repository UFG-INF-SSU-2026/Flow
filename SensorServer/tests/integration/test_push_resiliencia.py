"""
Resiliencia do push com servidor real: sem canal ativo, entrada tardia, reconexao com Last-Event-ID, cliente lento
e reinicio simulado do servidor.

Cenarios que dependem do prazo do episodio usam a fixture `relogio` (tempo congelado, avancado pelo teste).
O cenario do cliente que nao consome usa o relogio real, com mensagens de validade longa.
"""
import threading
import time
from types import SimpleNamespace

import pytest

from borda import auth
from borda import canal_app
from borda import canal_wokwi
from comum import const as const_comum
from borda import const as const_borda
from comum import mensagens
from analise import nucleo
from analise import regra_imobilidade
from analise import regra_luz
from analise import regra_queda
from borda import sse_hub
from _apoio import aguardar
from _cenarios import FluxoQueda
from borda.sse_hub import HUB, Hub


@pytest.fixture
def fluxo(relogio, servidor_real):
    return FluxoQueda(servidor_real, relogio)


@pytest.fixture
def abrir_sse(servidor_real):
    """abrir_sse(rota, token, headers=None) -> LeitorSSE. Fecha todos ao fim do teste."""
    abertos = []

    def _abrir(rota, token, headers=None):
        leitor = servidor_real.sse(rota, token, headers)
        abertos.append(leitor)
        return leitor

    yield _abrir
    for leitor in abertos:
        leitor.fechar()


def _linhas(ler_confirmacoes, tipo: str) -> list[dict]:
    return [l for l in ler_confirmacoes(1) if l["tipo"] == tipo]


def _unica_mensagem() -> str:
    """messageId do unico pedido emitido (sem precisar de um stream aberto para le-lo)."""
    (mid,) = regra_queda._mensagens
    return mid


# ------------------------------- sem canal ativo ------------------------------
def test_sem_canal_ativo_guarda_no_anel_e_o_episodio_expira(fluxo, relogio, ler_confirmacoes, banco):
    fluxo.enviar_queda()

    assert aguardar(lambda: len(banco.tabelas["comandos_push"]) >= 1)
    assert {canal for (_u, canal) in HUB._anel} == {canal_app.CANAL, canal_wokwi.CANAL}   # sem conexao: o anel guarda

    relogio.avancar(const_comum.TIMEOUT_CONFIRMACAO_S + const_comum.MARGEM_SERVIDOR_S)
    assert aguardar(lambda: len(banco.tabelas["comandos_push"]) >= 2)
    assert regra_queda._episodios[1].resultado == "EXPIRADO"
    assert [l["tipo"] for l in ler_confirmacoes(1)] == ["episodio.aberto", "episodio.encerrado"]
    assert _linhas(ler_confirmacoes, "episodio.encerrado")[0]["resultado"] == "EXPIRADO"


# ----------------------- conexao tardia, mas dentro da validade ----------------
def test_cliente_que_conecta_dentro_da_validade_recebe_o_pedido_com_timeout_reduzido(
        fluxo, relogio, abrir_sse, banco):
    fluxo.enviar_queda()                                     # ninguem conectado
    assert aguardar(lambda: len(banco.tabelas["comandos_push"]) >= 1)
    relogio.avancar(2)                                       # 2 s depois, ainda dentro do tempo do idoso

    leitor = abrir_sse("/push/app", fluxo.tok_app)
    leitor.esperar_comentario("conectado")
    pedido = leitor.proximo_push()

    assert pedido["messageType"] == mensagens.TIPO_SOLICITADA
    assert pedido["messageId"] == _unica_mensagem()
    assert pedido["payload"]["timeoutMs"] == (const_comum.TIMEOUT_CONFIRMACAO_S - 2) * 1000
    assert len(banco.tabelas["comandos_push"]) == 1          # o comando original nao muda (a borda nao regrava)


def test_pedido_com_tempo_do_cliente_esgotado_nao_e_reenviado(fluxo, relogio, abrir_sse):
    fluxo.enviar_queda()
    relogio.avancar(const_comum.TIMEOUT_CONFIRMACAO_S + 1)         # idoso sem tempo, mas ainda dentro da margem do servidor
    assert regra_queda._episodios[1].encerrado_em is None

    leitor = abrir_sse("/push/app", fluxo.tok_app)
    leitor.esperar_comentario("conectado")
    assert leitor.sem_push(0.5)


def test_mensagens_vencidas_nao_sao_reenviadas(fluxo, relogio, abrir_sse):
    fluxo.enviar_queda()
    fluxo.resposta_app(_unica_mensagem())                    # encerra: o anel passa a ter pedido e encerramento
    relogio.avancar(const_comum.TTL_ENCERRAMENTO_S + 1)            # os dois ja passaram do expiresAt

    leitor = abrir_sse("/push/app", fluxo.tok_app)
    leitor.esperar_comentario("conectado")
    assert leitor.sem_push(0.5)


# -------------------------- reconexao com Last-Event-ID -----------------------
def test_reconexao_com_last_event_id_recebe_so_o_que_faltou(fluxo, abrir_sse):
    leitor = abrir_sse("/push/app", fluxo.tok_app)
    leitor.esperar_comentario("conectado")
    fluxo.enviar_queda()
    pedido = leitor.proximo_push()
    visto = leitor.ultimo_id
    leitor.fechar()                                          # o app cai...
    fluxo.resposta_app(pedido["messageId"])                  # ...e o episodio encerra enquanto ele esta fora

    novo = abrir_sse("/push/app", fluxo.tok_app, {"Last-Event-ID": str(visto)})
    novo.esperar_comentario("conectado")
    encerramento = novo.proximo_push()

    assert encerramento["messageType"] == mensagens.TIPO_ENCERRADA
    assert encerramento["payload"]["resultado"] == "CONFIRMADO"
    assert encerramento["episodeId"] == pedido["episodeId"]
    assert novo.ultimo_id > visto
    assert novo.sem_push(0.5)                                # o pedido, ja visto, nao volta


def test_reconexao_sem_last_event_id_recebe_tudo_que_ainda_vale_em_ordem(fluxo, abrir_sse):
    leitor = abrir_sse("/push/app", fluxo.tok_app)
    leitor.esperar_comentario("conectado")
    fluxo.enviar_queda()
    pedido = leitor.proximo_push()
    leitor.fechar()
    fluxo.resposta_app(pedido["messageId"])

    novo = abrir_sse("/push/app", fluxo.tok_app)
    novo.esperar_comentario("conectado")
    primeiro, segundo = novo.proximo_push(), novo.proximo_push()

    assert (primeiro["messageType"], segundo["messageType"]) == (mensagens.TIPO_SOLICITADA, mensagens.TIPO_ENCERRADA)
    assert primeiro["messageId"] == pedido["messageId"]
    assert novo.sem_push(0.4)


# ------------------------------ cliente que nao consome ------------------------
def test_cliente_que_nao_consome_enche_a_fila_e_e_desconectado_e_reconecta_com_replay(
        servidor_real, abrir_sse, monkeypatch):
    token = servidor_real.login("idoso1", "senha-idoso", "app", "cel1")["access_token"]
    # O gerador do stream fica preso em _serializar enquanto o teste nao liberar: do ponto de vista do hub,
    # e um cliente que parou de consumir (a fila da conexao so se esvazia quando o gerador a le).
    entrou, liberar = threading.Event(), threading.Event()
    original = canal_app._serializar

    def preso(msg):
        entrou.set()
        liberar.wait(10)
        return original(msg)

    monkeypatch.setattr(canal_app, "_serializar", preso)
    leitor = abrir_sse("/push/app", token)
    leitor.esperar_comentario("conectado")

    msgs = [mensagens.criar_encerramento(1, f"ep-{i}", "CONFIRMADO") for i in range(const_borda.TAMANHO_FILA_CONEXAO + 2)]
    assert canal_app.enviar(1, msgs[0]) == "ENTREGUE_AO_CANAL"
    assert entrou.wait(3)                                    # o gerador pegou a primeira e esta preso com ela
    for m in msgs[1:-1]:                                     # as 16 seguintes ocupam a fila inteira
        assert canal_app.enviar(1, m) == "ENTREGUE_AO_CANAL"
    assert canal_app.enviar(1, msgs[-1]) == "SEM_CANAL_ATIVO"   # nao coube: conexao fechada, nao conta como entregue
    assert not sse_hub.HUB.ha_conexao(1, canal_app.CANAL)

    liberar.set()
    assert leitor.proximo_push()["messageId"] == msgs[0]["messageId"]
    visto = leitor.ultimo_id
    assert leitor.esperar_fim(3)                             # depois da primeira, o stream termina

    novo = abrir_sse("/push/app", token, {"Last-Event-ID": str(visto)})
    novo.esperar_comentario("conectado")
    recebidas, ids = [], []
    for _ in range(const_borda.TAMANHO_FILA_CONEXAO):
        recebidas.append(novo.proximo_push())
        ids.append(novo.ultimo_id)
    # pendentes: msgs[1:] (17); a fila comporta 16, entao valem as mais recentes: msgs[2:]
    assert [m["messageId"] for m in recebidas] == [m["messageId"] for m in msgs[2:]]
    assert ids == sorted(set(ids)) and ids[-1] == sse_hub.HUB._contador[(1, canal_app.CANAL)]
    assert novo.sem_push(0.4)


# ------------------------------ reinicio do servidor ---------------------------
@pytest.fixture
def reiniciar(monkeypatch, pipeline):
    """reiniciar() simula a troca dos processos: some o que estava em memoria (a analise, com o estado das regras e outro
    boot_id; as falhas de login; o hub SSE); o banco (com contas e sessoes) fica. O hub novo e criado alguns
    segundos depois do antigo, como seria num processo novo. Devolve o hub novo."""

    def _reiniciar() -> Hub:
        for tabela in (regra_queda._estados, regra_queda._ultimo_log_desatualizada, regra_queda._episodios,
                       regra_queda._mensagens, regra_queda._ultimo_ts_app, regra_imobilidade._estados,
                       regra_luz._estados, auth._falhas, auth._bloqueado_ate):
            tabela.clear()
        pipeline.reiniciar_analise(nucleo.PADRAO)
        with monkeypatch.context() as m:
            m.setattr(sse_hub, "time", SimpleNamespace(time=lambda: time.time() + 10))   # sse_hub so usa time.time()
            novo = Hub()
        monkeypatch.setattr(canal_app, "HUB", novo)
        monkeypatch.setattr(canal_wokwi, "HUB", novo)
        return novo

    return _reiniciar


def test_sessoes_persistem_no_reinicio(cli, reiniciar):
    sessao = cli.login("idoso1", "senha-idoso", "app", "cel1")
    reiniciar()

    r = cli.post("/auth/refresh", {"refresh_token": sessao["refresh_token"]})
    assert r.status_code == 200
    nova = cli.corpo(r)
    assert nova["refresh_token"] != sessao["refresh_token"] and nova["papel"] == "idoso" and nova["user_ids"] == [1]
    # o access token anterior segue valido (JWT sem estado no servidor)
    r = cli.post("/eventos", {"schemaVersion": const_comum.SCHEMA_VERSION_APP, "source": "app", "user_id": 1, "device_id": "cel1",
                              "event_time": "2026-10-14T12:00:00.000Z", "eventType": "push.ack", "state": "ATIVADO",
                              "messageId": "m-1"}, sessao["access_token"])
    assert r.status_code == 200


def test_last_event_id_antigo_nao_fica_a_frente_do_contador_depois_do_reinicio(fluxo, abrir_sse, reiniciar):
    leitor = abrir_sse("/push/app", fluxo.tok_app)
    leitor.esperar_comentario("conectado")
    fluxo.enviar_queda()
    leitor.proximo_push()
    visto = leitor.ultimo_id                                 # id do hub antigo, guardado pelo cliente
    leitor.fechar()

    reiniciar()
    novo = abrir_sse("/push/app", fluxo.tok_app, {"Last-Event-ID": str(visto)})
    novo.esperar_comentario("conectado")
    msg = mensagens.criar_encerramento(1, "ep-depois-do-reinicio", "EXPIRADO")
    assert canal_app.enviar(1, msg) == "ENTREGUE_AO_CANAL"

    recebida = novo.proximo_push()
    assert recebida["messageId"] == msg["messageId"]
    assert novo.ultimo_id > visto                            # o proximo Last-Event-ID do cliente nao "pula" mensagens


def test_estou_bem_de_antes_do_reinicio_encerra_o_episodio_restaurado_do_banco(fluxo, reiniciar, ler_confirmacoes):
    fluxo.enviar_queda()
    mid = _unica_mensagem()
    novo_hub = reiniciar()                                   # a analise perde o estado; o banco guarda o episodio e a mensagem
    assert regra_queda._episodios == {}

    fluxo.resposta_app(mid)                                  # o app responde ao pedido de antes do reinicio

    assert regra_queda._episodios[1].resultado == "CONFIRMADO"   # o estado foi restaurado do banco antes de a resposta valer
    ultima = ler_confirmacoes(1)[-1]
    assert ultima["tipo"] == "episodio.encerrado" and ultima["resultado"] == "CONFIRMADO"
    assert [m["payload"]["resultado"] for _, m in novo_hub._anel[(1, canal_app.CANAL)]
            if m["messageType"] == mensagens.TIPO_ENCERRADA] == ["CONFIRMADO"]
