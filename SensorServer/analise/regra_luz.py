"""
Regra de negocio: luz inadequada a noite (Marco 2 - fluxo priorizado).

Fluxo: LeituraAmbiente -> validacao -> deduplicacao -> regra de CCT -> alerta.

Contrato do evento (definido na Atividade 02):
    schemaVersion, device_id, event_time (ISO 8601 com timezone),
    luminosidade (lux), cct (Kelvin), seq_num (contador incremental por
    dispositivo, so para esse tipo de evento).

RELOGIO E ESTADO EXPLICITOS: esta regra nao le o relogio (a hora vem do `event_time` do proprio
evento) e o estado mora num `EstadoLuz` (parametro `estado=`; sem ele vale o `PADRAO` do modulo, de que `_estados`
e apelido).
"""

import threading
from dataclasses import dataclass, field
from datetime import datetime

from analise import const as const_analise   # CCT_*, HORA_*, LEITURAS_CONSECUTIVAS_PARA_ALERTA, MAX_ESTADOS_LEGADOS
from comum import const as const_comum       # SCHEMA_VERSION_*

# Versao do schema do payload LeituraAmbiente (mesma ideia aplicada ao
# contrato do wokwi, ver docs/arquitetura.md secao 4.3). O app atual
# (MainActivity.kt) envia a versao do contrato do app (const_comum.SCHEMA_VERSION_APP).
# Eventos sem o campo (app mais antigo/nao atualizado) sao tratados como
# versao 1 (const_comum.SCHEMA_VERSION_PADRAO) - nao sao rejeitados, apenas logados
# como desatualizados (ver validar_evento).


class EventoInvalido(Exception):
    """Evento fora do schema ou fora da faixa fisica plausivel."""


@dataclass
class EstadoDispositivo:
    """Estado mantido em memoria por device_id (nuvem, conforme Atividade 02)."""
    ultimo_seq_num: int | None = None
    leituras_consecutivas_condicao: int = 0
    alerta_ativo: bool = False
    ultimo_schema_version: int | None = None  # ultimo schemaVersion recebido (ou const_comum.SCHEMA_VERSION_PADRAO, se ausente)


# Estado por (user_id, device_id) (em memoria; reinicia se o servidor reiniciar -
# aceitavel nesta fase de prototipo, conforme decisao de simplicidade do grupo)
# Como o device_id vem do cliente, a tabela tem teto (const_analise.MAX_ESTADOS_LEGADOS): estoura, sai o menos usado
# (a ordem do dict e a do ultimo uso).
@dataclass
class EstadoLuz:
    """A tabela de estados da regra (uma instancia por 'mundo': laco da analise, simulacao, teste)."""
    estados: dict[tuple[int, str], EstadoDispositivo] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


PADRAO = EstadoLuz()
_estados = PADRAO.estados     # apelido da tabela do PADRAO (limpar uma limpa a outra)
_estados_lock = PADRAO.lock


def _estado_do(user_id: int, device_id: str, estado: "EstadoLuz | None" = None) -> EstadoDispositivo:
    tab = PADRAO if estado is None else estado
    chave = (user_id, device_id)
    with tab.lock:
        atual = tab.estados.pop(chave, None) or EstadoDispositivo()
        tab.estados[chave] = atual   # reinsere no fim: o primeiro do dict e sempre o menos usado
        while len(tab.estados) > const_analise.MAX_ESTADOS_LEGADOS:
            del tab.estados[next(iter(tab.estados))]
        return atual


def validar_evento(evento: dict) -> None:
    """Validacao SEMANTICA (a estrutura -- campos obrigatorios e tipos, schemas/app-leitura-ambiente.json -- ja foi
    conferida pela borda): faixa fisica plausivel e formato do instante. Levanta EventoInvalido se falhar."""
    # 0. schemaVersion: identificacao/aviso, nao rejeita (mesma ideia do wokwi, ver regra_imobilidade.validar_evento).
    #    App atual sempre manda SCHEMA_VERSION_APP; se ausente (app mais antigo), assume-se SCHEMA_VERSION_PADRAO.
    schema_version = evento.get("schemaVersion", const_comum.SCHEMA_VERSION_PADRAO)
    if isinstance(schema_version, int) and schema_version < const_comum.SCHEMA_VERSION_APP:
        print(
            f"  [app] aviso: evento leitura_ambiente com schemaVersion={schema_version} "
            f"desatualizado (atual={const_comum.SCHEMA_VERSION_APP}), "
            f"device_id={evento.get('device_id')}"
        )

    luminosidade = evento["luminosidade"]
    cct = evento["cct"]

    if luminosidade < 0:
        raise EventoInvalido(f"luminosidade fora de faixa: {luminosidade}")

    if not (const_analise.CCT_MIN_VALIDO <= cct <= const_analise.CCT_MAX_VALIDO):
        raise EventoInvalido(f"cct fora de faixa: {cct}")

    try:
        datetime.fromisoformat(evento["event_time"])
    except ValueError as e:
        raise EventoInvalido(f"event_time invalido: {evento['event_time']}") from e


def eh_duplicado(user_id: int, evento: dict, estado: EstadoLuz | None = None) -> bool:
    """Deduplicacao por (user_id, device_id) + seq_num (idempotencia)."""
    do_device = _estado_do(user_id, evento["device_id"], estado)
    seq = evento["seq_num"]
    if do_device.ultimo_seq_num is not None and seq <= do_device.ultimo_seq_num:
        return True
    do_device.ultimo_seq_num = seq
    return False


def _eh_noite(event_time: datetime) -> bool:
    hora = event_time.hour
    return hora >= const_analise.HORA_INICIO_NOITE or hora < const_analise.HORA_FIM_NOITE


def avaliar_regra(user_id: int, evento: dict, estado: EstadoLuz | None = None) -> dict | None:
    """
    Aplica a regra de luz inadequada (noite + CCT frio, com debounce).

    Retorna um dicionario de alerta se a condicao for confirmada nesta
    chamada, ou None caso contrario (inclui os casos "ainda nao confirmou"
    e "alerta ja estava ativo, nao repete").
    """
    device_id = evento["device_id"]
    do_device = _estado_do(user_id, device_id, estado)
    event_time = datetime.fromisoformat(evento["event_time"])

    condicao_presente = _eh_noite(event_time) and evento["cct"] > const_analise.CCT_LIMIAR_FRIO

    if not condicao_presente:
        # condicao nao se sustenta -> reseta contador e permite novo episodio no futuro
        do_device.leituras_consecutivas_condicao = 0
        do_device.alerta_ativo = False
        return None

    do_device.leituras_consecutivas_condicao += 1

    if do_device.alerta_ativo:
        # ja alertamos para este episodio, nao repete a cada 10s
        return None

    if do_device.leituras_consecutivas_condicao >= const_analise.LEITURAS_CONSECUTIVAS_PARA_ALERTA:
        do_device.alerta_ativo = True
        return {
            "tipo": "alerta_luz_inadequada",
            "device_id": device_id,
            "event_time": evento["event_time"],
            "luminosidade": evento["luminosidade"],
            "cct": evento["cct"],
            "seq_num": evento["seq_num"],
            "mensagem": (
                f"Luz fria (CCT={evento['cct']}K) detectada durante a noite "
                f"({const_analise.LEITURAS_CONSECUTIVAS_PARA_ALERTA} leituras consecutivas). "
                "Pode inibir a producao de melatonina do idoso."
            ),
        }

    return None
