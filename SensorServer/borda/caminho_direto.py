"""
Adaptador do CAMINHO DIRETO (roadmap do split, S2 a S5): a borda chama a analise no mesmo processo.

E o UNICO modulo da borda que conhece a analise (conferido por tests/unit/test_fronteiras.py). As rotas
(`borda/server.py`) so chamam as quatro funcoes publicas daqui:

    analisar_leitura(...)       /dados:   leitura aceita -> nucleo -> efeitos -> entrega de push
    analisar_resposta(...)      /eventos: resposta do dispositivo -> nucleo -> efeitos -> entrega de push
    estado_do_dispositivo(id)   /estado/<device_id>: resumo do estado da regra de imobilidade
    analise_monitoravel()       S6: True quando o /health deve acompanhar o batimento da analise (acoes_pelo_banco())
    iniciar_vigia()             thread que encerra episodios vencidos (o unico passo com relogio real)
    iniciar_laco()              S3a: thread do laco da analise por cursor (so registra decisoes em log; nao age)
    sombra_atual()              S3b: a coleta da comparacao em modo sombra (None se desligada)
    acoes_pelo_banco()          S4: True quando a analise age pelo banco (FLOW_ACOES_PELO_BANCO=1 e o laco ligado)
    iniciar_entrega()           S4: thread que le `comandos_push` e entrega por SSE (so com acoes_pelo_banco)

COM acoes_pelo_banco() (S4) este modulo deixa de decidir: analisar_leitura e analisar_resposta nao fazem nada (a leitura
ja foi para a fila de descarga e a resposta ja foi gravada em `respostas_dispositivo` pela rota; quem decide e o laco da
analise), o vigia nao sobe (e passo do laco) e estado_do_dispositivo le `estado_dispositivo`. Sem ele, tudo como sempre.

O que fica aqui e so o que e da borda: chamar o nucleo, entregar o push aos canais (SSE) e registrar o resultado da
entrega. A decisao e do nucleo (`analise/nucleo.py`) e a persistencia das decisoes e de `analise/efeitos.py`.
Na S4 este modulo e trocado pelo caminho por banco (a analise grava `comandos_push`; a borda entrega); o sinalizador
de volta ao caminho direto fica ate o fim da S5.
"""
import threading
import time
from datetime import datetime

from analise import const as const_analise
from analise import efeitos, laco, nucleo, regra_imobilidade, regra_queda, sombra
from borda import entrega_push, tempo, via_banco
from comum import const as const_comum
from flow_db import repositorios

# Canais de push, resultado de erro e traducao do resultado: os mesmos da entrega pelo banco (borda/entrega_push.py).
CANAIS = entrega_push.CANAIS
RESULTADO_ERRO_NO_CANAL = entrega_push.RESULTADO_ERRO_NO_CANAL
_RESULTADO_ENTREGA = entrega_push.RESULTADO_ENTREGA


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


def _observar_para_sombra(decisoes: "nucleo.Decisoes", origem: str) -> None:
    """S3b: entrega as decisoes do caminho direto a comparacao em modo sombra, ANTES dos efeitos (o `visto_em` e o
    momento da decisao, nao o fim da gravacao no banco). Nunca levanta (a Sombra ja se protege)."""
    coleta = _sombra
    if coleta is not None and decisoes:
        coleta.registrar(sombra.LADO_DIRETO, decisoes, origem)


def _aplicar(decisoes: "nucleo.Decisoes", origem: str = sombra.ORIGEM_LEITURA) -> None:
    """Efeitos das decisoes (analise/efeitos.py) e, depois de gravar cada comando, a entrega aos canais. A falha de
    um efeito nao impede os demais e nunca vira 500: a telemetria ja foi aceita. `origem` (leitura, resposta ou
    vigia) so serve a comparacao em modo sombra."""
    _observar_para_sombra(decisoes, origem)
    efeitos.aplicar(decisoes, entregar=lambda cmd: _entregar_comando(cmd))


# ---------------------------------------------------------------------------
# API para as rotas
# ---------------------------------------------------------------------------
def acoes_pelo_banco() -> bool:
    """S4: a analise age pelo banco (e a borda entrega a partir dele). Exige o laco ligado: sem ele ninguem decidiria,
    entao o sinalizador e ignorado e o caminho direto segue valendo. Lido na hora do uso."""
    return const_comum.ACOES_PELO_BANCO and const_analise.LACO_ATIVO


def analisar_leitura(user_id: int, device_id: str, fonte: str, dados: dict, versao: int, instante: datetime) -> None:
    """Leitura aceita pela borda -> nucleo -> efeitos. `instante` e o da ingestao (lido UMA vez pela rota).
    Falha ao decidir ou aplicar fica no log e nao vira 500. Com acoes_pelo_banco() nao faz nada (decide o laco)."""
    if acoes_pelo_banco():
        return
    try:
        _aplicar(nucleo.processar_leitura(nucleo.PADRAO, nucleo.Leitura(
            instante, user_id, device_id, fonte, dados, versao)), sombra.ORIGEM_LEITURA)
    except Exception as e:  # noqa: BLE001
        print(f"  [queda] falha ao avaliar ou emitir o pedido user={user_id}: {type(e).__name__}: {str(e)[:200]}")


def analisar_resposta(user_id: int, device_id: str | None, fonte: str, evento: dict, versao: int,
                      instante: datetime) -> None:
    """Resposta do dispositivo (evento ja normalizado) -> nucleo -> efeitos. O episodio ja esta encerrado no estado
    das regras; se o aviso aos dispositivos falhar, so ele se perde (log). Com acoes_pelo_banco() nao faz nada: a
    resposta ja esta em `respostas_dispositivo` e quem a correlaciona e a analise (DS1)."""
    if acoes_pelo_banco():
        return
    _aplicar(nucleo.processar_resposta(nucleo.PADRAO, nucleo.Resposta(
        instante, user_id, device_id, fonte, evento, versao)), sombra.ORIGEM_RESPOSTA)


def analise_monitoravel() -> bool:
    """S6: o /health so acompanha o batimento da analise quando ela decide pelo banco (acoes_pelo_banco()). No caminho
    direto quem decide e este processo, e o laco (se ligado) so registra: o batimento dele nao diz nada sobre as quedas."""
    return acoes_pelo_banco()


def estado_do_dispositivo(device_id: str) -> dict | None:
    """Resumo do estado da regra de imobilidade (procura em todos os usuarios), ou None se desconhecido. Com
    acoes_pelo_banco() le `estado_dispositivo` (DS3), onde a analise o grava; a forma da resposta e a mesma."""
    if acoes_pelo_banco():
        return via_banco.estado_do_dispositivo(device_id)
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
    _aplicar(nucleo.vigiar(nucleo.PADRAO, tempo.agora_utc()), sombra.ORIGEM_VIGIA)


def _vigia_loop() -> None:
    while True:
        time.sleep(const_analise.INTERVALO_VIGIA_S)
        try:
            _vigia_iteracao()
        except Exception as e:  # noqa: BLE001  (a thread nunca pode morrer em silencio)
            print(f"  [vigia] falha na iteracao (a thread segue): {type(e).__name__}: {str(e)[:200]}")


def iniciar_vigia() -> threading.Thread | None:
    """Sobe a thread do vigia (chamada uma unica vez por borda/server.iniciar_threads). Com acoes_pelo_banco() o
    vigia e um passo do laco da analise: nao sobe aqui."""
    if acoes_pelo_banco():
        return None
    t = threading.Thread(target=_vigia_loop, name="flow-vigia", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Laco da analise por cursor (S3a) e comparacao em modo sombra (S3b): rodam ao lado do caminho direto, SEM agir
# ---------------------------------------------------------------------------
_laco: "laco.LacoAnalise | None" = None
_sombra: "sombra.Sombra | None" = None


def sombra_atual() -> "sombra.Sombra | None":
    """A coleta da comparacao em modo sombra (S3b), ou None se desligada (FLOW_SOMBRA=1 liga)."""
    return _sombra


def _ativar_sombra() -> "sombra.Sombra | None":
    """S3b: cria a coleta (memoria + dataUsers/sombra_decisoes.jsonl) para o caminho direto e o laco a alimentarem.
    Exige o laco ligado: sem ele a sombra nunca decide e todo registro do caminho direto viraria `faltando`."""
    global _sombra
    if not const_analise.SOMBRA_ATIVA:
        return None
    if acoes_pelo_banco():
        print("  [sombra] FLOW_SOMBRA=1 ignorado: com FLOW_ACOES_PELO_BANCO=1 nao ha caminho direto para comparar")
        return None
    if not const_analise.LACO_ATIVO:
        print("  [sombra] FLOW_SOMBRA=1 ignorado: o laco da analise esta desligado (FLOW_LACO_ANALISE=0)")
        return None
    _sombra = sombra.Sombra(sombra.caminho_padrao())
    print(f"  [sombra] comparacao em modo sombra LIGADA; decisoes dos dois lados em {sombra.caminho_padrao()}")
    return _sombra


def iniciar_laco() -> threading.Thread | None:
    """Sobe o laco da analise (analise/laco.py) como thread do mesmo processo, com o papel flow_analise. Ele le
    `leituras`, `respostas_dispositivo` e `entregas_push` por cursor e so registra as decisoes em log: o caminho
    direto continua sendo o unico que age. Desligado por FLOW_LACO_ANALISE=0. Com FLOW_SOMBRA=1 (S3b) tambem entrega
    as decisoes a comparacao em modo sombra. Chamada uma unica vez por borda/server.iniciar_threads."""
    global _laco
    if not const_analise.LACO_ATIVO:
        print("  [laco] desligado (FLOW_LACO_ANALISE=0)")
        if const_comum.ACOES_PELO_BANCO:
            print("  [laco] AVISO: FLOW_ACOES_PELO_BANCO=1 ignorado sem o laco: o caminho direto segue decidindo")
        return None
    if const_comum.ACOES_PELO_BANCO:
        print("  [laco] ACOES PELO BANCO: a analise decide e grava; a borda entrega a partir de comandos_push (S4)")
    coleta = _ativar_sombra()
    observar = None if coleta is None else (lambda d, origem, rec: coleta.registrar(sombra.LADO_SOMBRA, d, origem, rec))
    if acoes_pelo_banco():
        _laco = laco.LacoAnalise(agir=True)
    elif coleta is None:
        _laco = laco.LacoAnalise()      # sem sombra: como na S3a
    else:
        _laco = laco.LacoAnalise(ao_observar=observar, traduzir_resposta=coleta.traduzir_resposta)
    return _laco.iniciar()


def parar_laco(timeout: float = 5.0) -> None:
    """Pede o fim do laco e espera o ciclo em curso terminar (desligamento ordenado, S5)."""
    if _laco is not None:
        _laco.parar(timeout)


# ---------------------------------------------------------------------------
# Entrega do push pelo banco (S4): a borda le `comandos_push` por cursor
# ---------------------------------------------------------------------------
_entrega: "entrega_push.EntregaPush | None" = None


def iniciar_entrega() -> threading.Thread | None:
    """Sobe a thread que le `comandos_push` por cursor e entrega por SSE (borda/entrega_push.py). So com
    acoes_pelo_banco(): no caminho direto quem entrega e `_despachar`, e duas entregas duplicariam o push. Chamada
    uma unica vez por borda/server.iniciar_threads."""
    global _entrega
    if not acoes_pelo_banco():
        return None
    _entrega = entrega_push.EntregaPush()
    return _entrega.iniciar()


def parar_entrega(timeout: float = 5.0) -> None:
    """Pede o fim da entrega e espera o ciclo em curso terminar (desligamento ordenado, S5)."""
    if _entrega is not None:
        _entrega.parar(timeout)


# ---------------------------------------------------------------------------
# Interface comum dos pipelines da borda (S5): `borda/server.py` so chama estas duas, qualquer que seja o pipeline
# (este, no processo unico, ou `borda/via_banco.py`, na borda isolada).
# ---------------------------------------------------------------------------
def iniciar() -> None:
    """Sobe vigia, laco da analise e entrega (cada um decide por si se deve subir). Uma unica vez, por server.iniciar_threads."""
    iniciar_vigia()    # encerra episodios vencidos
    iniciar_laco()     # S3a/S4
    iniciar_entrega()  # S4: so com FLOW_ACOES_PELO_BANCO=1


def parar(timeout: float = 5.0) -> None:
    """Desligamento ordenado: o ciclo do laco e o da entrega em curso terminam (o lote chega ao commit do cursor)."""
    parar_laco(timeout)
    parar_entrega(timeout)
