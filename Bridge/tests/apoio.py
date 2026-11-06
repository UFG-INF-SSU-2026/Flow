"""Mensagens de push de exemplo para os testes do bridge."""
import uuid
from datetime import datetime, timedelta, timezone

TIPO_SOLICITADA = "queda.confirmacao_solicitada"
TIPO_ENCERRADA = "queda.confirmacao_encerrada"
USER_ID = 1


# ------------------------------------------------------------------ apoio
def _iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def pedido(**campos) -> dict:
    """Mensagem 'queda.confirmacao_solicitada' valida; `campos` sobrescreve (ou, com valor ausente, use _sem)."""
    agora = datetime.now(timezone.utc)
    msg = {
        "schemaVersion": 3,
        "messageId": str(uuid.uuid4()),
        "messageType": TIPO_SOLICITADA,
        "userId": USER_ID,
        "episodeId": str(uuid.uuid4()),
        "issuedAt": _iso_z(agora),
        "expiresAt": _iso_z(agora + timedelta(seconds=10)),
        "payload": {"timeoutMs": 5000, "motivo": "IMPACTO_SEGUIDO_DE_IMOBILIDADE"},
    }
    msg.update(campos)
    return msg


def encerramento(resultado="CONFIRMADO", **campos) -> dict:
    msg = pedido(messageType=TIPO_ENCERRADA, payload={"resultado": resultado})
    msg.update(campos)
    return msg


def _sem(msg: dict, chave: str) -> dict:
    return {k: v for k, v in msg.items() if k != chave}
