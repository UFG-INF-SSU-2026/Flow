"""
S4: os cenarios do roadmap (simular_cenarios.py) com as ACOES PELO BANCO, sem Docker e sem rede.

Servidor do teste (Flask test_client + banco falso) com FLOW_ACOES_PELO_BANCO ligado: as rotas so aceitam e gravam
(`leituras`, `respostas_dispositivo`); quem decide e grava `comandos_push`, `alertas`, `confirmacoes`, `episodios` e
`estado_dispositivo` e o laco da analise (ciclos sincronos, relogio virtual); quem entrega e a `EntregaPush` da borda, que
le `comandos_push` por cursor. O caminho direto fica parado (o estado das regras dele nao se mexe).
"""
import pytest

import simular_cenarios as sc
from _cenarios_sombra import TransporteInterno
from analise import const as const_analise
from analise import laco, nucleo
from borda import entrega_push
from comum import const as const_comum
from comum import mensagens

NOMES = [c.nome for c in sc.cenarios()]


class CanalCapturador:
    """Canal de push que guarda (instante virtual, mensagem) do que a borda entregou."""

    def __init__(self, nome, relogio):
        self.CANAL, self._relogio, self.recebidos = nome, relogio, []

    def enviar(self, user_id, mensagem):
        self.recebidos.append((self._relogio.agora(), mensagem))
        return "ENTREGUE_AO_CANAL"


@pytest.fixture
def mundo(relogio, banco, cli, tokens, monkeypatch):
    monkeypatch.setattr(const_comum, "ACOES_PELO_BANCO", True)
    monkeypatch.setattr(const_analise, "LACO_ATIVO", True)
    lc = laco.LacoAnalise(obter_repo=lambda: banco.analise(), relogio=relogio.agora_utc, ao_decidir=lambda *_: None,
                          agir=True)
    app, wokwi = CanalCapturador("app", relogio), CanalCapturador("wokwi", relogio)
    ent = entrega_push.EntregaPush(obter_repo=lambda: banco.borda(), relogio=relogio.agora_utc, canais=[app, wokwi])
    transporte = TransporteInterno(relogio, cli, {"app": tokens["idoso"], "wokwi": tokens["dispositivo"]}, banco, lc, ent)
    sessao = sc.Sessao(transporte, 1, "bridge-dev", "cel1", seq_inicial=1000)

    def rodar(nome):
        cenario = next(c for c in sc.cenarios() if c.nome == nome)
        transporte.iniciar_cenario()
        sc.executar(cenario, sessao)
        assert all(status == 200 for _a, _r, status in sessao.status), sessao.status
        return transporte.t0

    return rodar, app, wokwi


def tipos(banco, tabela):
    return [l["tipo"] for l in banco.tabelas[tabela]]


def resultados(banco):
    return [l["payload"]["payload"]["resultado"] for l in banco.tabelas["comandos_push"]
            if l["tipo"] == mensagens.TIPO_ENCERRADA]


def test_todo_cenario_tem_conferencia_propria():
    assert set(CONFERE) == set(NOMES)


def _descartes(banco):
    assert not banco.tabelas["comandos_push"] and not banco.tabelas["alertas"] and not banco.tabelas["episodios"]
    assert banco.tabelas["estado_dispositivo"]


def _luz(banco):
    assert [a["tipo"] for a in banco.tabelas["alertas"]] == ["luz_inadequada"] and not banco.tabelas["comandos_push"]


def _estou_bem(banco):
    assert tipos(banco, "comandos_push") == [mensagens.TIPO_SOLICITADA, mensagens.TIPO_ENCERRADA]
    assert resultados(banco) == ["CONFIRMADO"]
    (ep,) = banco.tabelas["episodios"]
    assert (ep["estado"], ep["motivo_encerramento"]) == ("encerrado", "CONFIRMADO")
    assert {"episodio.aberto", "push.ack", "confirmacao.resposta", "episodio.encerrado"} <= set(tipos(banco, "confirmacoes"))


def _sem_resposta(banco):
    assert resultados(banco) == ["EXPIRADO"]
    assert [e["motivo_encerramento"] for e in banco.tabelas["episodios"]] == ["EXPIRADO"]


def _tardia(banco):
    assert resultados(banco) == ["EXPIRADO"]
    assert any(c["dados"].get("tardia") for c in banco.tabelas["confirmacoes"] if c["tipo"] == "confirmacao.resposta")


def _imobilidade(banco):
    assert [a["tipo"] for a in banco.tabelas["alertas"]] == ["imobilidade"]
    assert banco.tabelas["comandos_push"][0]["payload"]["payload"]["motivo"] == const_analise.MOTIVO_B
    assert resultados(banco) == ["CONFIRMADO"]


def _juntos(banco):
    assert banco.tabelas["comandos_push"][0]["payload"]["payload"]["motivo"] == const_analise.MOTIVO_A
    aberto = next(c for c in banco.tabelas["confirmacoes"] if c["tipo"] == "episodio.aberto")
    assert {f["fonte"] for f in aberto["dados"]["fontes_usadas"]} == {"wokwi", "app"}
    assert resultados(banco) == ["CONFIRMADO"]


CONFERE = {"descartes_wokwi": _descartes, "luz_inadequada": _luz, "queda_estou_bem": _estou_bem,
           "queda_sem_resposta": _sem_resposta, "queda_resposta_tardia": _tardia,
           "imobilidade_gatilho_b": _imobilidade, "app_e_wokwi_juntos": _juntos}


@pytest.mark.parametrize("nome", NOMES)
def test_o_cenario_funciona_com_as_acoes_pelo_banco(nome, mundo, banco):
    rodar, app, wokwi = mundo
    rodar(nome)

    CONFERE[nome](banco)
    # O caminho direto ficou parado: o estado das regras dele nao viu nada.
    assert not nucleo.PADRAO.queda.estados and not nucleo.PADRAO.queda.episodios
    # Todo comando do banco foi entregue aos DOIS canais e cada entrega tem a sua linha (com comando_id).
    n = len(banco.tabelas["comandos_push"])
    assert len(app.recebidos) == len(wokwi.recebidos) == n
    assert len(banco.tabelas["entregas_push"]) == 2 * n
    assert {e["comando_id"] for e in banco.tabelas["entregas_push"]} == {c["id"] for c in banco.tabelas["comandos_push"]}
    assert {e["resultado"] for e in banco.tabelas["entregas_push"]} <= {"enviado", "expirado"}


def test_o_pedido_chega_ao_dispositivo_dentro_do_orcamento_de_latencia(mundo, banco):
    """Da leitura que dispara o pedido ate a entrega: no modelo, um passo (0,25 s) <= orcamento (0,75 s)."""
    rodar, app, _wokwi = mundo
    t0 = rodar("queda_estou_bem")
    instante_do_gatilho = t0 + 1 + const_analise.T_POS_IMPACTO_S
    pedidos = [(t, m) for t, m in app.recebidos if m["messageType"] == mensagens.TIPO_SOLICITADA]
    assert len(pedidos) == 1
    assert 0 < pedidos[0][0] - instante_do_gatilho <= const_analise.ORCAMENTO_LATENCIA_S


def test_o_dispositivo_responde_ao_messageId_que_recebeu(mundo, banco):
    """O id que a borda entregou e o do banco: a analise reconhece a resposta sem nenhuma traducao (a da S3b some)."""
    rodar, app, _w = mundo
    rodar("queda_estou_bem")
    (pedido,) = [m for _t, m in app.recebidos if m["messageType"] == mensagens.TIPO_SOLICITADA]
    assert pedido["messageId"] == banco.tabelas["comandos_push"][0]["message_id"]
    resposta = next(c for c in banco.tabelas["confirmacoes"] if c["tipo"] == "confirmacao.resposta")
    assert resposta["message_id"] == pedido["messageId"] and not resposta["dados"].get("mensagem_desconhecida")


def test_a_rota_de_estado_le_o_estado_gravado_pela_analise(mundo, banco, cli, tokens):
    rodar, _a, _w = mundo
    rodar("descartes_wokwi")
    r = cli.get("/estado/bridge-dev", tokens["admin"])
    corpo = cli._json(r)
    assert r.status_code == 200 and corpo["device_id"] == "bridge-dev" and corpo["user_id"] == 1
    assert cli.get("/estado/desconhecido", tokens["admin"]).status_code == 404


def test_a_borda_nao_escreve_decisoes_so_o_que_e_dela(mundo, banco):
    rodar, _a, _w = mundo
    rodar("queda_estou_bem")
    for tabela in ("comandos_push", "confirmacoes", "episodios", "estado_dispositivo"):
        assert banco.tabelas[tabela]
    assert not banco.tabelas["alertas"]      # queda confirmada nao gera alerta
    assert banco.tabelas["leituras"] and banco.tabelas["respostas_dispositivo"] and banco.tabelas["entregas_push"]
