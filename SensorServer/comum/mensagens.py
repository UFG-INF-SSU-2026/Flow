"""
Contrato da mensagem de push (comum a quem PRODUZ, a analise, e a quem ENTREGA, a borda). Marco 3, sem I/O de rede: mensagem de push servidor -> dispositivo (contrato schemas/push-mensagem.json,
const_comum.SCHEMA_VERSION_MENSAGEM, docs/schemas.md secao 6). A validacao dos eventos de retorno dispositivo -> servidor
(push.ack, confirmacao.resposta) e estrutural e vive na borda (validacao.validar_evento_retorno).

Relogio: quem DECIDE (analise/nucleo.py) sempre passa `agora` (o instante da ingestao) a criar_pedido_confirmacao e
criar_encerramento. O `agora_utc()` padrao so serve a quem nao passa instante: o replay da entrega por SSE
(`para_envio`, que mede o tempo restante AGORA, na borda) e as chamadas avulsas.
"""
import uuid
from datetime import datetime, timedelta, timezone

from comum import const as const_comum   # SCHEMA_VERSION_MENSAGEM, MARGEM_SERVIDOR_S, TTL_ENCERRAMENTO_S (lidos na hora do uso)

TIPO_SOLICITADA = "queda.confirmacao_solicitada"
TIPO_ENCERRADA = "queda.confirmacao_encerrada"
RESULTADOS_ENCERRAMENTO = {"CONFIRMADO", "EXPIRADO", "CANCELADO"}

def agora_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_iso_z(texto: str) -> datetime:
    return datetime.strptime(texto, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def criar_pedido_confirmacao(user_id: int, episode_id: str, timeout_s: float, motivo: str,
                             agora: datetime | None = None) -> dict:
    agora = agora or agora_utc()
    return {
        "schemaVersion": const_comum.SCHEMA_VERSION_MENSAGEM,
        "messageId": str(uuid.uuid4()),
        "messageType": TIPO_SOLICITADA,
        "userId": user_id,
        "episodeId": episode_id,
        "issuedAt": iso_z(agora),
        "expiresAt": iso_z(agora + timedelta(seconds=timeout_s + const_comum.MARGEM_SERVIDOR_S)),
        "payload": {"timeoutMs": int(timeout_s * 1000), "motivo": motivo},
    }


def criar_encerramento(user_id: int, episode_id: str, resultado: str, agora: datetime | None = None) -> dict:
    if resultado not in RESULTADOS_ENCERRAMENTO:
        raise ValueError(resultado)
    agora = agora or agora_utc()
    return {
        "schemaVersion": const_comum.SCHEMA_VERSION_MENSAGEM,
        "messageId": str(uuid.uuid4()),
        "messageType": TIPO_ENCERRADA,
        "userId": user_id,
        "episodeId": episode_id,
        "issuedAt": iso_z(agora),
        "expiresAt": iso_z(agora + timedelta(seconds=const_comum.TTL_ENCERRAMENTO_S)),
        "payload": {"resultado": resultado},
    }


def para_envio(msg: dict, agora: datetime | None = None) -> dict | None:
    """Copia pronta para ir ao dispositivo, ou None se a mensagem ja expirou.
    Para pedidos de confirmacao, RECALCULA payload.timeoutMs como o tempo que resta
    (importante no replay apos reconexao)."""
    agora = agora or agora_utc()
    restante_ms = int((parse_iso_z(msg["expiresAt"]) - agora).total_seconds() * 1000)
    if restante_ms <= 0:
        return None
    copia = {**msg, "payload": dict(msg["payload"])}
    if msg["messageType"] == TIPO_SOLICITADA:
        original = msg["payload"]["timeoutMs"]
        emitido = parse_iso_z(msg["issuedAt"])
        restante_cliente = original - int((agora - emitido).total_seconds() * 1000)
        if restante_cliente <= 0:
            return None
        copia["payload"]["timeoutMs"] = min(original, restante_cliente)
    return copia
