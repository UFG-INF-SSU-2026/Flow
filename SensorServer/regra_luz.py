"""
Regra de negocio: luz inadequada a noite (Marco 2 - fluxo priorizado).

Fluxo: LeituraAmbiente -> validacao -> deduplicacao -> regra de CCT -> alerta.

Contrato do evento (definido na Atividade 02):
    device_id, event_time (ISO 8601 com timezone), luminosidade (lux),
    cct (Kelvin), seq_num (contador incremental por dispositivo, so para
    esse tipo de evento).
"""

from dataclasses import dataclass, field
from datetime import datetime

# ---------------------------------------------------------------------------
# CONFIGURACOES DA REGRA (ajuste aqui se necessario)
# ---------------------------------------------------------------------------
CCT_MIN_VALIDO = 1000        # Kelvin - abaixo disso, leitura e descartada (fora de faixa)
CCT_MAX_VALIDO = 12000       # Kelvin - acima disso, leitura e descartada (fora de faixa)
CCT_LIMIAR_FRIO = 4500       # Kelvin - acima disso, luz e considerada "fria" (rica em azul)
HORA_INICIO_NOITE = 18       # 18h
HORA_FIM_NOITE = 6           # 06h (janela noturna: 18:00-06:00)
LEITURAS_CONSECUTIVAS_PARA_ALERTA = 3   # debounce: evita alertar por 1 leitura isolada
# ---------------------------------------------------------------------------


class EventoInvalido(Exception):
    """Evento fora do schema ou fora da faixa fisica plausivel."""


@dataclass
class EstadoDispositivo:
    """Estado mantido em memoria por device_id (nuvem, conforme Atividade 02)."""
    ultimo_seq_num: int | None = None
    leituras_consecutivas_condicao: int = 0
    alerta_ativo: bool = False


# Estado por dispositivo (em memoria; reinicia se o servidor reiniciar -
# aceitavel nesta fase de prototipo, conforme decisao de simplicidade do grupo)
_estados: dict[str, EstadoDispositivo] = {}


def _estado_do(device_id: str) -> EstadoDispositivo:
    if device_id not in _estados:
        _estados[device_id] = EstadoDispositivo()
    return _estados[device_id]


def validar_evento(evento: dict) -> None:
    """Valida schema + faixa fisica plausivel. Levanta EventoInvalido se falhar."""
    campos_obrigatorios = ["device_id", "event_time", "luminosidade", "cct", "seq_num"]
    for campo in campos_obrigatorios:
        if campo not in evento:
            raise EventoInvalido(f"Campo obrigatorio ausente: {campo}")

    luminosidade = evento["luminosidade"]
    cct = evento["cct"]

    if not isinstance(luminosidade, (int, float)) or luminosidade < 0:
        raise EventoInvalido(f"luminosidade fora de faixa: {luminosidade}")

    if not isinstance(cct, (int, float)) or not (CCT_MIN_VALIDO <= cct <= CCT_MAX_VALIDO):
        raise EventoInvalido(f"cct fora de faixa: {cct}")

    try:
        datetime.fromisoformat(evento["event_time"])
    except ValueError as e:
        raise EventoInvalido(f"event_time invalido: {evento['event_time']}") from e


def eh_duplicado(evento: dict) -> bool:
    """Deduplicacao por device_id + seq_num (idempotencia)."""
    estado = _estado_do(evento["device_id"])
    seq = evento["seq_num"]
    if estado.ultimo_seq_num is not None and seq <= estado.ultimo_seq_num:
        return True
    estado.ultimo_seq_num = seq
    return False


def _eh_noite(event_time: datetime) -> bool:
    hora = event_time.hour
    return hora >= HORA_INICIO_NOITE or hora < HORA_FIM_NOITE


def avaliar_regra(evento: dict) -> dict | None:
    """
    Aplica a regra de luz inadequada (noite + CCT frio, com debounce).

    Retorna um dicionario de alerta se a condicao for confirmada nesta
    chamada, ou None caso contrario (inclui os casos "ainda nao confirmou"
    e "alerta ja estava ativo, nao repete").
    """
    device_id = evento["device_id"]
    estado = _estado_do(device_id)
    event_time = datetime.fromisoformat(evento["event_time"])

    condicao_presente = _eh_noite(event_time) and evento["cct"] > CCT_LIMIAR_FRIO

    if not condicao_presente:
        # condicao nao se sustenta -> reseta contador e permite novo episodio no futuro
        estado.leituras_consecutivas_condicao = 0
        estado.alerta_ativo = False
        return None

    estado.leituras_consecutivas_condicao += 1

    if estado.alerta_ativo:
        # ja alertamos para este episodio, nao repete a cada 10s
        return None

    if estado.leituras_consecutivas_condicao >= LEITURAS_CONSECUTIVAS_PARA_ALERTA:
        estado.alerta_ativo = True
        return {
            "tipo": "alerta_luz_inadequada",
            "device_id": device_id,
            "event_time": evento["event_time"],
            "luminosidade": evento["luminosidade"],
            "cct": evento["cct"],
            "seq_num": evento["seq_num"],
            "mensagem": (
                f"Luz fria (CCT={evento['cct']}K) detectada durante a noite "
                f"({LEITURAS_CONSECUTIVAS_PARA_ALERTA} leituras consecutivas). "
                "Pode inibir a producao de melatonina do idoso."
            ),
        }

    return None
