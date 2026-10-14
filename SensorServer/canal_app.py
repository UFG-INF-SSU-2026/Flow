"""Canal servidor -> app (SSE). Rota GET /push/app. Interface comum dos canais: enviar(user_id, mensagem)."""
import json

from flask import Blueprint, g, request

import mensagens
from authz import exige
from crypto_utils import encrypt_payload
from sse_hub import HUB, ler_last_event_id

CANAL = "app"
SUPORTA = {mensagens.TIPO_SOLICITADA, mensagens.TIPO_ENCERRADA}
bp = Blueprint("canal_app", __name__)


def _serializar(msg: dict) -> str | None:
    pronta = mensagens.para_envio(msg)
    if pronta is None:
        return None
    nonce, ct = encrypt_payload(json.dumps(pronta))
    return json.dumps({"nonce": nonce, "ciphertext": ct})


@bp.route("/push/app", methods=["GET"])
@exige("push:receber", fonte="app")
def stream_app():
    p = g.principal
    last = ler_last_event_id(request.headers.get("Last-Event-ID"))   # invalido vira 0 (DC-13)
    return HUB.resposta_sse(p.user_ids[0], CANAL, p.device_id or "app", p.exp, last, _serializar)


def enviar(user_id: int, mensagem: dict) -> str:
    if mensagem["messageType"] not in SUPORTA:
        return "TIPO_NAO_SUPORTADO"
    return "ENTREGUE_AO_CANAL" if HUB.publicar(user_id, CANAL, mensagem) > 0 else "SEM_CANAL_ATIVO"
