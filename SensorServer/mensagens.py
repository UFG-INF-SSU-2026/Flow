"""
Mensagens do Marco 3 (sem I/O de rede):
  - mensagem de push servidor -> dispositivo (schema de mensagem, const.SCHEMA_VERSION_MENSAGEM, docs/schemas.md secao 3)
  - validacao dos eventos de retorno dispositivo -> servidor (push.ack, confirmacao.resposta)
"""
import uuid
from datetime import datetime, timedelta, timezone

import const   # SCHEMA_VERSION_MENSAGEM, FONTES_VALIDAS, MARGEM_SERVIDOR_S, TTL_ENCERRAMENTO_S (lidos na hora do uso)

TIPO_SOLICITADA = "queda.confirmacao_solicitada"
TIPO_ENCERRADA = "queda.confirmacao_encerrada"
RESULTADOS_ENCERRAMENTO = {"CONFIRMADO", "EXPIRADO", "CANCELADO"}

EVENTOS_RETORNO = {
    "push.ack": {"ATIVADO", "REJEITADO"},
    "confirmacao.resposta": {"ESTOU_BEM", "SEM_RESPOSTA"},
}


class EventoRetornoInvalido(Exception):
    pass


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
        "schemaVersion": const.SCHEMA_VERSION_MENSAGEM,
        "messageId": str(uuid.uuid4()),
        "messageType": TIPO_SOLICITADA,
        "userId": user_id,
        "episodeId": episode_id,
        "issuedAt": iso_z(agora),
        "expiresAt": iso_z(agora + timedelta(seconds=timeout_s + const.MARGEM_SERVIDOR_S)),
        "payload": {"timeoutMs": int(timeout_s * 1000), "motivo": motivo},
    }


def criar_encerramento(user_id: int, episode_id: str, resultado: str, agora: datetime | None = None) -> dict:
    if resultado not in RESULTADOS_ENCERRAMENTO:
        raise ValueError(resultado)
    agora = agora or agora_utc()
    return {
        "schemaVersion": const.SCHEMA_VERSION_MENSAGEM,
        "messageId": str(uuid.uuid4()),
        "messageType": TIPO_ENCERRADA,
        "userId": user_id,
        "episodeId": episode_id,
        "issuedAt": iso_z(agora),
        "expiresAt": iso_z(agora + timedelta(seconds=const.TTL_ENCERRAMENTO_S)),
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


def validar_evento_retorno(payload: dict, source: str) -> dict:
    """Valida e normaliza um evento de POST /eventos. Retorna dict normalizado:
    {eventType, state, messageId, device_id, source}. Levanta EventoRetornoInvalido.

    `source` deve ser 'app' ou 'wokwi' e JA conferida com o token pelo chamador (authz.exigir_escopo).
    Qualquer outro valor e erro de programacao (ValueError), nao conteudo invalido do cliente."""
    if source not in const.FONTES_VALIDAS:
        raise ValueError(f"source deve ser uma de {sorted(const.FONTES_VALIDAS)} (ja conferida com o token): {source!r}")
    tipo = payload.get("eventType")
    if tipo not in EVENTOS_RETORNO:
        raise EventoRetornoInvalido(f"eventType desconhecido: {tipo}")
    state = payload.get("state")
    if state not in EVENTOS_RETORNO[tipo]:
        raise EventoRetornoInvalido(f"state '{state}' nao esperado para '{tipo}'")
    message_id = payload.get("messageId")
    if not isinstance(message_id, str) or not message_id:
        raise EventoRetornoInvalido("messageId ausente")
    device_id = payload.get("device_id") or payload.get("deviceId")
    if not device_id:
        raise EventoRetornoInvalido("device_id/deviceId ausente")
    if source == "wokwi":
        for campo in ("entityId", "eventTimeMs", "sequence"):
            if campo not in payload:
                raise EventoRetornoInvalido(f"campo obrigatorio ausente (wokwi): {campo}")
    else:  # source == "app"
        if "event_time" not in payload:
            raise EventoRetornoInvalido("campo obrigatorio ausente (app): event_time")
    return {"eventType": tipo, "state": state, "messageId": message_id,
            "device_id": str(device_id), "source": source}
