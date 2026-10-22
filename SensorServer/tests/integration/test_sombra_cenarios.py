"""
S3b: comparacao em modo sombra, cenario por cenario, SEM Docker e SEM rede.

Os cenarios sao os de `simular_cenarios.py` (os mesmos que o script roda contra o servidor real). Aqui o servidor e o
do teste (Flask test_client + banco falso), o relogio e virtual e o laco da analise roda em ciclos sincronos. O que se
confere em cada cenario:
  1. o cenario FEZ o que diz (um episodio confirmado, um expirado, um alerta de luz...): sem isso comparar "nada com
     nada" passaria sem provar coisa alguma;
  2. a sombra produziu as MESMAS decisoes do caminho direto: sem decisao faltante e sem decisao a mais, com os instantes
     dentro da tolerancia;
  3. o laco nao agiu (quem escreveu `comandos_push` foi so o caminho direto).
O atraso aqui e o do modelo (relogio virtual). O atraso real mede-se rodando simular_cenarios.py contra o servidor.
"""
import json

import pytest

import simular_cenarios as sc
from _cenarios_sombra import TransporteInterno, montar_laco
from analise import const as const_analise
from analise import sombra
from borda import caminho_direto
from comum import mensagens

NOMES = [c.nome for c in sc.cenarios()]


@pytest.fixture
def mundo(relogio, banco, cli, tokens, monkeypatch):
    """Fabrica de mundos: `criar(traduzir=True)` -> (transporte, sessao, coleta)."""
    def criar(traduzir: bool = True):
        coleta = sombra.Sombra(relogio=relogio.agora)
        monkeypatch.setattr(caminho_direto, "_sombra", coleta)
        lc = montar_laco(relogio, banco, coleta, traduzir)
        transporte = TransporteInterno(relogio, cli, {"app": tokens["idoso"], "wokwi": tokens["dispositivo"]}, banco, lc)
        sessao = sc.Sessao(transporte, 1, "bridge-dev", "cel1", seq_inicial=1000)
        return transporte, sessao, coleta
    return criar


def rodar(nome: str, criar, traduzir: bool = True):
    cenario = next(c for c in sc.cenarios() if c.nome == nome)
    transporte, sessao, coleta = criar(traduzir)
    transporte.iniciar_cenario()
    sc.executar(cenario, sessao)
    assert all(status == 200 for _a, _r, status in sessao.status), sessao.status
    return coleta, sombra.comparar(coleta.registros())


def chaves(coleta, tipo: str, lado: str = sombra.LADO_DIRETO) -> list[dict]:
    return [json.loads(r.chave) for r in coleta.registros() if r.lado == lado and r.tipo == tipo]


def mensagens_de(coleta, tipo_msg: str) -> list[dict]:
    return [c for c in chaves(coleta, sombra.T_COMANDO) if c["messageType"] == tipo_msg]


def descreve(res) -> str:
    return "\n".join(f"{d.tipo} {d.registro} user={d.user_id} {d.detalhe} {d.chave[:140]}" for d in res.divergencias)


# ---------------------------------------------------------------------------------------------------- o que cada um faz
def _confere_descartes_wokwi(coleta):
    motivos = {c["motivo"] for c in chaves(coleta, sombra.T_DESCARTE)}
    assert "duplicada" in motivos and len(motivos) >= 2          # a repetida e a fora de faixa
    assert not chaves(coleta, sombra.T_COMANDO) and not chaves(coleta, sombra.T_ALERTA)


def _confere_luz(coleta):
    (alerta,) = chaves(coleta, sombra.T_ALERTA)
    assert alerta["tipo"] == "luz_inadequada"
    assert {"duplicada", "semantica_invalida"} <= {c["motivo"] for c in chaves(coleta, sombra.T_DESCARTE)}


def _encerramentos(coleta) -> list[str]:
    return [c["payload"]["resultado"] for c in mensagens_de(coleta, mensagens.TIPO_ENCERRADA)]


def _confere_estou_bem(coleta):
    assert len(mensagens_de(coleta, mensagens.TIPO_SOLICITADA)) == 1
    assert _encerramentos(coleta) == ["CONFIRMADO"]
    tipos = {c["tipo"] for c in chaves(coleta, sombra.T_CONFIRMACAO)}
    assert {"episodio.aberto", "push.ack", "confirmacao.resposta", "episodio.encerrado"} <= tipos


def _confere_sem_resposta(coleta):
    assert _encerramentos(coleta) == ["EXPIRADO"]
    assert any(r.origem == sombra.ORIGEM_VIGIA and r.tipo == sombra.T_COMANDO
               for r in coleta.registros() if r.lado == sombra.LADO_DIRETO)
    assert any(r.origem == sombra.ORIGEM_VIGIA and r.tipo == sombra.T_COMANDO
               for r in coleta.registros() if r.lado == sombra.LADO_SOMBRA)       # a sombra tambem expirou


def _confere_tardia(coleta):
    assert _encerramentos(coleta) == ["EXPIRADO"]
    assert any(c["dados"].get("tardia") for c in chaves(coleta, sombra.T_CONFIRMACAO) if c["tipo"] == "confirmacao.resposta")


def _confere_imobilidade(coleta):
    (alerta,) = chaves(coleta, sombra.T_ALERTA)
    assert alerta["tipo"] == "imobilidade"
    (pedido,) = mensagens_de(coleta, mensagens.TIPO_SOLICITADA)
    assert pedido["payload"]["motivo"] == const_analise.MOTIVO_B
    assert _encerramentos(coleta) == ["CONFIRMADO"]


def _confere_juntos(coleta):
    (pedido,) = mensagens_de(coleta, mensagens.TIPO_SOLICITADA)
    assert pedido["payload"]["motivo"] == const_analise.MOTIVO_A
    aberto = next(c for c in chaves(coleta, sombra.T_CONFIRMACAO) if c["tipo"] == "episodio.aberto")
    assert {f[0] for f in aberto["dados"]["fontes_usadas"]} == {"wokwi", "app"}      # a fusao usou as duas fontes
    assert _encerramentos(coleta) == ["CONFIRMADO"]


CONFERE = {"descartes_wokwi": _confere_descartes_wokwi, "luz_inadequada": _confere_luz,
           "queda_estou_bem": _confere_estou_bem, "queda_sem_resposta": _confere_sem_resposta,
           "queda_resposta_tardia": _confere_tardia, "imobilidade_gatilho_b": _confere_imobilidade,
           "app_e_wokwi_juntos": _confere_juntos}


def test_todo_cenario_tem_conferencia_propria():
    assert set(CONFERE) == set(NOMES)


@pytest.mark.parametrize("nome", NOMES)
def test_a_sombra_decide_o_mesmo_que_o_caminho_direto(nome, mundo, banco):
    coleta, res = rodar(nome, mundo)

    CONFERE[nome](coleta)
    assert res.sem_divergencia, descreve(res)
    assert res.pendentes == 0 and res.reconstrucoes == 0
    total = lambda campo: sum(v[campo] for v in res.contagem.values())    # noqa: E731
    assert total("direto") > 0 and total("direto") == total("sombra") == total("pareados")
    atraso = res.atraso()
    assert atraso["n"] > 0 and res.atraso_dentro_do_orcamento             # modelo: um passo de ciclo (0,25 s)
    # O laco nao agiu: todo comando gravado e do caminho direto (um por comando decidido por ele).
    assert len(banco.tabelas["comandos_push"]) == len(chaves(coleta, sombra.T_COMANDO))


# ---------------------------------------------------------------------------------------------------- guardas do comparador
def test_sem_a_traducao_dos_message_id_a_sombra_nao_reconhece_a_resposta_e_diverge(mundo):
    """Documenta por que `traduzir_resposta` existe: cada lado sorteia os seus ids e o dispositivo so conhece os do
    caminho direto. Sem a traducao o comparador DEVE acusar (e acusa: confirmacao, encerramento e origem)."""
    _coleta, res = rodar("queda_estou_bem", mundo, traduzir=False)
    assert not res.sem_divergencia
    assert {d.tipo for d in res.divergencias} & {sombra.D_FALTANDO, sombra.D_A_MAIS}


def test_um_laco_com_limiar_diferente_e_pego_pela_comparacao(mundo, monkeypatch):
    """O comparador nao e tautologico: se o laco decidir diferente do caminho direto, a divergencia aparece."""
    transporte, sessao, coleta = mundo()
    cenario = next(c for c in sc.cenarios() if c.nome == "queda_estou_bem")
    # So o laco passa a ignorar impactos: o caminho direto continua abrindo o episodio.
    original = sombra.registros_de

    def sem_pedidos(decisoes, lado, origem, reconstrucao=False, visto_em=None):
        regs = original(decisoes, lado, origem, reconstrucao, visto_em)
        return [r for r in regs if not (lado == sombra.LADO_SOMBRA and r.tipo in (sombra.T_COMANDO, sombra.T_EPISODIO))]

    monkeypatch.setattr(sombra, "registros_de", sem_pedidos)
    transporte.iniciar_cenario()
    sc.executar(cenario, sessao)
    res = sombra.comparar(coleta.registros())
    assert any(d.tipo == sombra.D_FALTANDO and d.registro == sombra.T_COMANDO for d in res.divergencias)
