"""
Traducao pura: mensagem de push do servidor -> linha de comando do firmware, e o parser SSE. Nao faz rede nem conhece
serial, Wokwi ou Flask. O bridge nao decide nada: valida a forma, traduz e entrega.

Comando (o mesmo formato serve a serial e a API):
    CMD;BUZZER_ON;<messageId>;<timeoutMs>
    CMD;BUZZER_OFF;<messageId>;<resultado>
"""
import re
from collections import deque
from datetime import datetime, timezone

EVENTOS_RETORNO = {"push.ack", "confirmacao.resposta"}      # vao para /eventos; o resto, para /dados
TIPO_SOLICITADA = "queda.confirmacao_solicitada"
TIPO_ENCERRADA = "queda.confirmacao_encerrada"
RESULTADOS_ENCERRAMENTO = {"CONFIRMADO", "EXPIRADO", "CANCELADO"}
# messageId vira campo da linha do comando: so caracteres de UUID e no maximo 39 (o buffer do firmware tem 40 bytes, o
# UUID tem 36). Usado com fullmatch: '$' aceitaria uma quebra de linha no fim.
MESSAGE_ID_RE = re.compile(r"[A-Za-z0-9-]{1,39}")
# device_id vira parte da rota e do log: letras, digitos, ponto, sublinhado e hifen.
DEVICE_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
MENSAGENS_VISTAS_MAX = 100


def rota_do_evento(evento: dict) -> str:
    return "/eventos" if evento.get("eventType") in EVENTOS_RETORNO else "/dados"


def forma_invalida(msg: dict) -> str | None:
    """Motivo pelo qual a mensagem NAO pode virar comando, ou None se a forma esta correta.

    Protege o enquadramento do comando (CMD;...;<fim de linha>): um ';' ou quebra de linha no messageId, um timeoutMs que
    nao e inteiro ou um resultado fora do contrato nunca chegam ao firmware. Tipo desconhecido passa (comando_do_push o
    ignora adiante). NAO filtra a FAIXA do timeoutMs: o firmware responde push.ack REJEITADO fora de 0 < t <= BUZZER_MAX_MS,
    e esse ack e a informacao util para o servidor.
    """
    mid = msg.get("messageId")
    if not isinstance(mid, str) or MESSAGE_ID_RE.fullmatch(mid) is None:
        return "messageId invalido"
    tipo = msg.get("messageType")
    if not isinstance(tipo, str):
        return "messageType ausente ou nao e texto"
    if tipo not in (TIPO_SOLICITADA, TIPO_ENCERRADA):
        return None
    payload = msg.get("payload")
    if not isinstance(payload, dict):
        return "payload ausente ou nao e objeto"
    if tipo == TIPO_SOLICITADA:
        t = payload.get("timeoutMs")
        if not isinstance(t, int) or isinstance(t, bool):       # bool e int em Python: True nao e prazo
            return "timeoutMs nao e inteiro"
    else:
        resultado = payload.get("resultado")
        if not isinstance(resultado, str) or resultado not in RESULTADOS_ENCERRAMENTO:
            return "resultado fora do contrato"
    return None


def traduzir(msg: dict) -> str | None:
    """Mensagem de push (v3) -> linha de comando. None se o tipo nao e suportado. Espera uma mensagem que ja passou por
    forma_invalida; sozinha, nao valida nada."""
    tipo, p = msg["messageType"], msg["payload"]
    if tipo == TIPO_SOLICITADA:
        return f"CMD;BUZZER_ON;{msg['messageId']};{int(p['timeoutMs'])}"
    if tipo == TIPO_ENCERRADA:
        return f"CMD;BUZZER_OFF;{msg['messageId']};{p['resultado']}"
    return None


def expirada(msg: dict) -> bool:
    exp = datetime.strptime(msg["expiresAt"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= exp


def comando_do_push(msg: dict, user_id: int, vistas: deque) -> tuple[str | None, str]:
    """Decide o que fazer com uma mensagem de push ja decifrada: (comando ou None, motivo para o log).

    Descarta schema errado, userId de outra conta, forma invalida, repetida (idempotencia na reconexao), expirada e tipo nao
    suportado. O motivo nunca traz o conteudo da mensagem (o texto veio da rede)."""
    if msg.get("schemaVersion") != 3:
        return None, f"schemaVersion {msg.get('schemaVersion')}"
    if msg.get("userId") != user_id:
        return None, "userId diferente do deste dispositivo"
    motivo = forma_invalida(msg)
    if motivo is not None:
        return None, motivo
    if msg["messageId"] in vistas:
        return None, "repetida"
    vistas.append(msg["messageId"])
    if expirada(msg):
        return None, "expirada"
    cmd = traduzir(msg)
    return (cmd, "ok") if cmd is not None else (None, "tipo nao suportado")


def ler_eventos(linhas):
    """Parser SSE puro: recebe as linhas (sem o fim de linha) e gera um dict por evento COMPLETO.

    Um evento termina na linha em branco; comentarios (':keepalive') sao ignorados; um evento que ficou sem a linha em
    branco final (conexao cortada no meio) nao e gerado."""
    ev: dict = {}
    for linha in linhas:
        if linha == "":                                  # fim de um evento SSE
            if ev:
                yield ev
            ev = {}
        elif not linha.startswith(":"):                  # ':' = comentario/keepalive
            k, _, v = linha.partition(":")
            ev[k] = v.strip()
