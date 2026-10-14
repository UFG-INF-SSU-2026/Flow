"""
Regra de negocio: luz inadequada a noite (Marco 2 - fluxo priorizado).

Fluxo: LeituraAmbiente -> validacao -> deduplicacao -> regra de CCT -> alerta.

Contrato do evento (definido na Atividade 02):
    schemaVersion, device_id, event_time (ISO 8601 com timezone),
    luminosidade (lux), cct (Kelvin), seq_num (contador incremental por
    dispositivo, so para esse tipo de evento).
"""

from dataclasses import dataclass, field
from datetime import datetime

import const   # CCT_*, HORA_*, LEITURAS_CONSECUTIVAS_PARA_ALERTA, SCHEMA_VERSION_* (ver const.py)

# Versao do schema do payload LeituraAmbiente (mesma ideia aplicada ao
# contrato do wokwi, ver docs/arquitetura.md secao 4.3). O app atual
# (MainActivity.kt) sempre envia schemaVersion=3 (const.SCHEMA_VERSION_ATUAL).
# Eventos sem o campo (app mais antigo/nao atualizado) sao tratados como
# versao 1 (const.SCHEMA_VERSION_PADRAO) - nao sao rejeitados, apenas logados
# como desatualizados (ver validar_evento).


class EventoInvalido(Exception):
    """Evento fora do schema ou fora da faixa fisica plausivel."""


@dataclass
class EstadoDispositivo:
    """Estado mantido em memoria por device_id (nuvem, conforme Atividade 02)."""
    ultimo_seq_num: int | None = None
    leituras_consecutivas_condicao: int = 0
    alerta_ativo: bool = False
    ultimo_schema_version: int | None = None  # ultimo schemaVersion recebido (ou const.SCHEMA_VERSION_PADRAO, se ausente)


# Estado por dispositivo (em memoria; reinicia se o servidor reiniciar -
# aceitavel nesta fase de prototipo, conforme decisao de simplicidade do grupo)
_estados: dict[str, EstadoDispositivo] = {}


def _estado_do(device_id: str) -> EstadoDispositivo:
    if device_id not in _estados:
        _estados[device_id] = EstadoDispositivo()
    return _estados[device_id]


def validar_evento(evento: dict) -> None:
    """Valida schema + faixa fisica plausivel. Levanta EventoInvalido se falhar."""
    # 0. schemaVersion: identificacao/aviso, nao rejeita (mesma ideia do
    #    wokwi, ver regra_imobilidade.validar_evento). App atual sempre
    #    manda 3; se ausente (app mais antigo), assume-se SCHEMA_VERSION_PADRAO.
    schema_version = evento.get("schemaVersion", const.SCHEMA_VERSION_PADRAO)
    if schema_version < const.SCHEMA_VERSION_ATUAL:
        print(
            f"  [app] aviso: evento leitura_ambiente com schemaVersion={schema_version} "
            f"desatualizado (atual={const.SCHEMA_VERSION_ATUAL}), "
            f"device_id={evento.get('device_id')}"
        )

    campos_obrigatorios = ["device_id", "event_time", "luminosidade", "cct", "seq_num"]
    for campo in campos_obrigatorios:
        if campo not in evento:
            raise EventoInvalido(f"Campo obrigatorio ausente: {campo}")

    luminosidade = evento["luminosidade"]
    cct = evento["cct"]

    if not isinstance(luminosidade, (int, float)) or luminosidade < 0:
        raise EventoInvalido(f"luminosidade fora de faixa: {luminosidade}")

    if not isinstance(cct, (int, float)) or not (const.CCT_MIN_VALIDO <= cct <= const.CCT_MAX_VALIDO):
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
    return hora >= const.HORA_INICIO_NOITE or hora < const.HORA_FIM_NOITE


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

    condicao_presente = _eh_noite(event_time) and evento["cct"] > const.CCT_LIMIAR_FRIO

    if not condicao_presente:
        # condicao nao se sustenta -> reseta contador e permite novo episodio no futuro
        estado.leituras_consecutivas_condicao = 0
        estado.alerta_ativo = False
        return None

    estado.leituras_consecutivas_condicao += 1

    if estado.alerta_ativo:
        # ja alertamos para este episodio, nao repete a cada 10s
        return None

    if estado.leituras_consecutivas_condicao >= const.LEITURAS_CONSECUTIVAS_PARA_ALERTA:
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
                f"({const.LEITURAS_CONSECUTIVAS_PARA_ALERTA} leituras consecutivas). "
                "Pode inibir a producao de melatonina do idoso."
            ),
        }

    return None
