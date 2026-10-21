"""
Adaptador do CAMINHO DIRETO (roadmap do split, S2 a S5): a borda chama a analise no mesmo processo.

E o UNICO modulo da borda que conhece a analise (conferido por tests/unit/test_fronteiras.py). As rotas
(`borda/server.py`) so chamam as quatro funcoes publicas daqui:

    analisar_leitura(...)       /dados:   leitura aceita -> nucleo -> efeitos -> entrega de push
    analisar_resposta(...)      /eventos: resposta do dispositivo -> nucleo -> efeitos -> entrega de push
    estado_do_dispositivo(id)   /estado/<device_id>: resumo do estado da regra de imobilidade
    iniciar_vigia()             thread que encerra episodios vencidos (o unico passo com relogio real)
    iniciar_laco()              S3a: thread do laco da analise por cursor (so registra decisoes em log; nao age)

O que fica aqui e so o que e da borda: chamar o nucleo, entregar o push aos canais (SSE) e registrar o resultado da
entrega. A decisao e do nucleo (`analise/nucleo.py`) e a persistencia das decisoes e de `analise/efeitos.py`.
Na S4 este modulo e trocado pelo caminho por banco (a analise grava `comandos_push`; a borda entrega); o sinalizador
de volta ao caminho direto fica ate o fim da S5.
"""
import threading
import time
from datetime import datetime

from analise import const as const_analise
from analise import efeitos, laco, nucleo, regra_imobilidade, regra_queda
from borda import canal_app, canal_wokwi, tempo
from flow_db import repositorios

# Canais de push (servidor -> dispositivo). Todos expoem CANAL e enviar(user_id, mensagem).
CANAIS = [canal_app, canal_wokwi]

RESULTADO_ERRO_NO_CANAL = "ERRO_NO_CANAL"   # canal.enviar levantou excecao (alem de ENTREGUE_AO_CANAL etc. dos canais)

_RESULTADO_ENTREGA = {"ENTREGUE_AO_CANAL": "enviado", "SEM_CANAL_ATIVO": "sem_conexao",
                      "TIPO_NAO_SUPORTADO": "nao_suportado", "ERRO_NO_CANAL": "erro"}


def _registro_para_banco(user_id: int, tipo: str, linha: dict) -> None:
    """Ouvinte de regra_queda.registrar: o que antes so ia para confirmacoes_usuario_<id>.jsonl tambem vai ao banco.
    push.entrega -> entregas_push (escrita da borda); os demais (episodio.*, push.ack, confirmacao.resposta) ->
    confirmacoes (escrita da analise)."""
    if tipo == "push.entrega":
        instante = datetime.fromisoformat(linha["timestamp"])
        repositorios.tentar_borda("entrega", lambda b: b.gravar_entrega({
            "instante": instante, "comando_id": None, "message_id": linha.get("messageId"), "user_id": user_id,
            "device_id": None, "canal": linha.get("canal"),
            "resultado": _RESULTADO_ENTREGA.get(linha.get("resultado"), "erro"), "detalhe": linha.get("resultado")}))
        return
    efeitos.gravar_confirmacao_banco(user_id, tipo, linha)


regra_queda.OUVINTE_REGISTRO = _registro_para_banco


# ---------------------------------------------------------------------------
# Entrega do push (servidor -> dispositivo)
# ---------------------------------------------------------------------------
def _despachar(user_id: int, msg: dict) -> None:
    """Entrega a mesma mensagem a TODOS os canais; registra o resultado por canal.
    Cada canal tem a sua propria protecao: a falha de um (envio ou gravacao do resultado) fica no log e
    nao impede os demais."""
    for canal in CANAIS:
        try:
            resultado = canal.enviar(user_id, msg)
        except Exception as e:  # noqa: BLE001
            resultado = RESULTADO_ERRO_NO_CANAL
            print(f"  [push] falha no canal={canal.CANAL} user={user_id}: {type(e).__name__}: {str(e)[:200]}")
        try:
            regra_queda.registrar(user_id, "push.entrega", {
                "episodeId": msg["episodeId"], "messageId": msg["messageId"],
                "messageType": msg["messageType"], "canal": canal.CANAL, "resultado": resultado})
        except Exception as e:  # noqa: BLE001
            print(f"  [push] falha ao registrar push.entrega canal={canal.CANAL} user={user_id}: "
                  f"{type(e).__name__}: {str(e)[:200]}")
        print(f"  [push] {msg['messageType']} user={user_id} canal={canal.CANAL} -> {resultado}")


def _entregar_comando(cmd: "nucleo.Comando") -> None:
    """Entrega um comando ja gravado aos canais."""
    _despachar(cmd.user_id, cmd.mensagem)


def _aplicar(decisoes: "nucleo.Decisoes") -> None:
    """Efeitos das decisoes (analise/efeitos.py) e, depois de gravar cada comando, a entrega aos canais. A falha de
    um efeito nao impede os demais e nunca vira 500: a telemetria ja foi aceita."""
    efeitos.aplicar(decisoes, entregar=lambda cmd: _entregar_comando(cmd))


# ---------------------------------------------------------------------------
# API para as rotas
# ---------------------------------------------------------------------------
def analisar_leitura(user_id: int, device_id: str, fonte: str, dados: dict, versao: int, instante: datetime) -> None:
    """Leitura aceita pela borda -> nucleo -> efeitos. `instante` e o da ingestao (lido UMA vez pela rota).
    Falha ao decidir ou aplicar fica no log e nao vira 500."""
    try:
        _aplicar(nucleo.processar_leitura(nucleo.PADRAO, nucleo.Leitura(
            instante, user_id, device_id, fonte, dados, versao)))
    except Exception as e:  # noqa: BLE001
        print(f"  [queda] falha ao avaliar ou emitir o pedido user={user_id}: {type(e).__name__}: {str(e)[:200]}")


def analisar_resposta(user_id: int, device_id: str | None, fonte: str, evento: dict, versao: int,
                      instante: datetime) -> None:
    """Resposta do dispositivo (evento ja normalizado) -> nucleo -> efeitos. O episodio ja esta encerrado no estado
    das regras; se o aviso aos dispositivos falhar, so ele se perde (log)."""
    _aplicar(nucleo.processar_resposta(nucleo.PADRAO, nucleo.Resposta(
        instante, user_id, device_id, fonte, evento, versao)))


def estado_do_dispositivo(device_id: str) -> dict | None:
    """Resumo do estado da regra de imobilidade (procura em todos os usuarios), ou None se desconhecido."""
    return regra_imobilidade.obter_estado(device_id, estado=nucleo.PADRAO.imobilidade)


# ---------------------------------------------------------------------------
# Entradas avulsas (testes, ferramentas): o fluxo normal nao passa por aqui
# ---------------------------------------------------------------------------
def _emitir_pedido(user_id: int, pedido: "regra_queda.PedidoConfirmacao", device_id: str | None = None) -> None:
    """Monta o comando de um pedido ja aberto, no instante de agora, grava e entrega."""
    cmd = nucleo.comando_de_pedido(nucleo.PADRAO, user_id, pedido, device_id, tempo.agora_utc())
    efeitos.gravar_comando_banco(cmd)
    _entregar_comando(cmd)


def _encerrar(enc: "regra_queda.Encerramento") -> None:
    """Monta o comando de encerramento no instante de agora, grava e entrega."""
    cmd = nucleo.comando_de_encerramento(enc, tempo.agora_utc())
    efeitos.gravar_comando_banco(cmd)
    _entregar_comando(cmd)


# ---------------------------------------------------------------------------
# Vigia
# ---------------------------------------------------------------------------
def _vigia_iteracao() -> None:
    """Uma passada do vigia: o nucleo encerra os episodios vencidos (relogio REAL, o unico passo que o usa) e o
    adaptador aplica e despacha o encerramento de cada um. Cada decisao tem a sua propria protecao."""
    _aplicar(nucleo.vigiar(nucleo.PADRAO, tempo.agora_utc()))


def _vigia_loop() -> None:
    while True:
        time.sleep(const_analise.INTERVALO_VIGIA_S)
        try:
            _vigia_iteracao()
        except Exception as e:  # noqa: BLE001  (a thread nunca pode morrer em silencio)
            print(f"  [vigia] falha na iteracao (a thread segue): {type(e).__name__}: {str(e)[:200]}")


def iniciar_vigia() -> threading.Thread:
    """Sobe a thread do vigia (chamada uma unica vez por borda/server.iniciar_threads)."""
    t = threading.Thread(target=_vigia_loop, name="flow-vigia", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Laco da analise por cursor (S3a): roda ao lado do caminho direto, SEM agir
# ---------------------------------------------------------------------------
_laco: "laco.LacoAnalise | None" = None


def iniciar_laco() -> threading.Thread | None:
    """Sobe o laco da analise (analise/laco.py) como thread do mesmo processo, com o papel flow_analise. Ele le
    `leituras`, `respostas_dispositivo` e `entregas_push` por cursor e so registra as decisoes em log: o caminho
    direto continua sendo o unico que age. Desligado por FLOW_LACO_ANALISE=0. Chamada uma unica vez por
    borda/server.iniciar_threads."""
    global _laco
    if not const_analise.LACO_ATIVO:
        print("  [laco] desligado (FLOW_LACO_ANALISE=0)")
        return None
    _laco = laco.LacoAnalise()
    return _laco.iniciar()


def parar_laco(timeout: float = 5.0) -> None:
    """Pede o fim do laco e espera o ciclo em curso terminar (desligamento ordenado, S5)."""
    if _laco is not None:
        _laco.parar(timeout)
