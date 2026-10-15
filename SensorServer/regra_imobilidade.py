"""
Regra de negocio: decisao de imobilidade do wokwi (Marco 2/3 - fecha a
pendencia do consumidor registrada em docs/relatorio.md, secao "Consumidor").

Fluxo: evento wokwi (imobilidade.leitura | imobilidade.decisao) ->
validacao em cascata -> deduplicacao -> atualizacao de estado persistente
-> atuacao (quando state == "ALERTA_IMOBILIDADE").

Contrato do evento (emitido por sketch/sketch.ino, funcao emitirEvento):
    schemaVersion, eventType, deviceId, entityId, eventTimeMs, sequence, value, unit, state

Este modulo espelha deliberadamente a estrutura de regra_luz.py (mesma
separacao validar_evento / eh_duplicado / avaliar-e-atuar), para que as
duas regras do servidor sigam o mesmo padrao e fiquem faceis de explicar
lado a lado na verificacao oral.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

import const   # ACEL_MIN/MAX_PLAUSIVEL, SCHEMA_VERSION_ATUAL/PADRAO (ver const.py)

# Versao do schema do payload wokwi (docs/arquitetura.md, secao 4.3).
# O produtor atual (sketch.ino) sempre envia schemaVersion=3 (const.SCHEMA_VERSION_ATUAL).
# Eventos sem o campo (produtor mais antigo/nao atualizado) sao tratados como
# versao 1 (const.SCHEMA_VERSION_PADRAO) - nao sao rejeitados, apenas logados
# como desatualizados (ver validar_evento).

# Vocabulario conhecido: para cada eventType, quais valores de "state" sao
# aceitos. Vem direto da maquina de estados de sketch.ino (nomeEstado() +
# os literais passados para emitirEvento() em cada transicao).
EVENTOS_ESPERADOS = {
    "imobilidade.leitura": {"NORMAL", "AGUARDANDO_CONFIRMACAO", "ALERTA_CONFIRMADO"},
    "imobilidade.decisao": {
        "LEITURA_INVALIDA",
        "LEITURA_FORA_DE_FAIXA",
        "SUSPEITA_IMOBILIDADE",
        "CONFIRMADO_OK",
        "MOVIMENTO_RETOMADO",
        "ALERTA_IMOBILIDADE",
        "REARMADO_MANUAL",
    },
}

# States de decisao que, ao chegar, encerram um episodio de alerta em aberto
# (o dispositivo voltou a um estado seguro) - usados para "rearmar" o flag
# alerta_ativo do lado do servidor, simetrico ao alerta_ativo do firmware.
ESTADOS_QUE_ENCERRAM_ALERTA = {"CONFIRMADO_OK", "MOVIMENTO_RETOMADO", "REARMADO_MANUAL"}

CAMPOS_OBRIGATORIOS = [
    "eventType", "deviceId", "entityId", "eventTimeMs", "sequence", "value", "unit", "state",
]


class EventoInvalido(Exception):
    """Evento fora do vocabulario conhecido, do enum esperado ou da faixa fisica plausivel."""


@dataclass
class EstadoDispositivo:
    """Estado mantido em memoria por deviceId - ao contrario do buffer de
    agregacao de server.py (que e limpo a cada flush de 1s), esta tabela
    persiste entre janelas e e o que resolve a pendencia "mudanca de
    estado" do relatorio."""
    ultimo_sequence: int | None = None
    estado_atual: str = "DESCONHECIDO"      # ultimo state de imobilidade.leitura (heartbeat)
    ultima_decisao: str | None = None       # ultimo state de imobilidade.decisao
    alerta_ativo: bool = False              # evita reemitir arquivo de alerta para o mesmo episodio
    ultima_atualizacao: str | None = None   # ISO 8601, para saber ha quanto tempo o device fala com o servidor
    ultimo_schema_version: int | None = None  # ultimo schemaVersion recebido (ou const.SCHEMA_VERSION_PADRAO, se ausente)


# Estado por (user_id, device_id) (em memoria; reinicia se o servidor reiniciar,
# mesma decisao de simplicidade ja assumida em regra_luz.py). O user_id faz parte da chave: dois usuarios com o
# mesmo device_id nao compartilham estado nem sequence.
_estados: dict[tuple[int, str], EstadoDispositivo] = {}


def _estado_do(user_id: int, device_id: str) -> EstadoDispositivo:
    chave = (user_id, device_id)
    if chave not in _estados:
        _estados[chave] = EstadoDispositivo()
    return _estados[chave]


def _agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validar_evento(evento: dict) -> None:
    """Cascata de validacao (planejada no relatorio, item "Consumidor"):

        0. schemaVersion (apenas identificacao/aviso, nao rejeita)
        1. eventType pertence ao vocabulario conhecido
        2. state pertence ao enum esperado daquele eventType
        3. value dentro da faixa fisica plausivel (0-160 m/s2)

    A faixa fisica (passo 3) e verificada de forma coerente com o que o
    proprio firmware envia: LEITURA_INVALIDA usa o sentinela -1.0 (leitura
    falhou, nao ha magnitude) e LEITURA_FORA_DE_FAIXA reporta magnitude
    real acima do teto (>160) - validar esses dois contra 0-160 rejeitaria
    exatamente os eventos que existem para *avisar* sobre a anomalia.
    Levanta EventoInvalido se qualquer passo falhar.
    """
    # 0. schemaVersion: primeiro passo da cascata (docs/arquitetura.md, 4.3).
    #    Produtor atual sempre manda 2; se o campo nao vier (produtor mais
    #    antigo), assume-se SCHEMA_VERSION_PADRAO (1). Uma versao desatualizada
    #    nao invalida o evento - so gera um aviso explicito no console, para
    #    dar visibilidade de que ha um produtor fora da versao atual em campo.
    schema_version = evento.get("schemaVersion", const.SCHEMA_VERSION_PADRAO)
    if schema_version < const.SCHEMA_VERSION_ATUAL:
        print(
            f"  [wokwi] aviso: evento com schemaVersion={schema_version} "
            f"desatualizado (atual={const.SCHEMA_VERSION_ATUAL}), "
            f"deviceId={evento.get('deviceId')}"
        )

    for campo in CAMPOS_OBRIGATORIOS:
        if campo not in evento:
            raise EventoInvalido(f"Campo obrigatorio ausente: {campo}")

    event_type = evento["eventType"]
    state = evento["state"]
    value = evento["value"]

    # 1. eventType conhecido
    if event_type not in EVENTOS_ESPERADOS:
        raise EventoInvalido(f"eventType desconhecido: {event_type}")

    # 2. state esperado para esse eventType
    if state not in EVENTOS_ESPERADOS[event_type]:
        raise EventoInvalido(
            f"state '{state}' nao esperado para eventType '{event_type}' "
            f"(esperado um de {sorted(EVENTOS_ESPERADOS[event_type])})"
        )

    if not isinstance(value, (int, float)):
        raise EventoInvalido(f"value nao numerico: {value!r}")

    # sequence precisa ser inteiro: eh_duplicado compara com o ultimo (seq <= ultimo). Um valor nao numerico
    # aceito aqui seria gravado como referencia e faria TODO evento seguinte do dispositivo falhar.
    sequence = evento["sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        raise EventoInvalido(f"sequence nao inteiro: {sequence!r}")

    # 3. faixa fisica plausivel, com as duas excecoes explicitas do firmware
    if state == "LEITURA_INVALIDA":
        if value != -1.0:
            raise EventoInvalido(
                f"LEITURA_INVALIDA deveria vir com o sentinela -1.0, veio {value}"
            )
    elif state == "LEITURA_FORA_DE_FAIXA":
        if value <= const.ACEL_MAX_PLAUSIVEL:
            raise EventoInvalido(
                f"LEITURA_FORA_DE_FAIXA deveria vir com value > {const.ACEL_MAX_PLAUSIVEL}, veio {value}"
            )
    else:
        if not (const.ACEL_MIN_PLAUSIVEL <= value <= const.ACEL_MAX_PLAUSIVEL):
            raise EventoInvalido(
                f"value fora da faixa fisica plausivel "
                f"({const.ACEL_MIN_PLAUSIVEL}-{const.ACEL_MAX_PLAUSIVEL}): {value}"
            )


def eh_duplicado(user_id: int, evento: dict) -> bool:
    """Deduplicacao/idempotencia: sequence deve ser maior que o ultimo
    conhecido para aquele (user_id, deviceId) (sequence e global por dispositivo no
    firmware, nao por eventType - um unico contador incrementa a cada
    emitirEvento(), seja leitura ou decisao)."""
    device_id = str(evento["deviceId"])
    estado = _estado_do(user_id, device_id)
    seq = evento["sequence"]

    if estado.ultimo_sequence is not None and seq <= estado.ultimo_sequence:
        return True

    estado.ultimo_sequence = seq
    return False


def processar_evento(user_id: int, evento: dict) -> dict | None:
    """Atualiza a tabela de estado persistente e, quando aplicavel, produz
    a atuacao observavel (alerta) para state == "ALERTA_IMOBILIDADE".

    Retorna um dicionario de alerta se uma nova atuacao deve ser
    registrada nesta chamada, ou None caso contrario (inclui os casos
    "e so o heartbeat", "decisao nao critica" e "alerta ja estava ativo
    para este episodio").
    """
    device_id = str(evento["deviceId"])
    estado = _estado_do(user_id, device_id)
    estado.ultima_atualizacao = _agora_iso()
    estado.ultimo_schema_version = evento.get("schemaVersion", const.SCHEMA_VERSION_PADRAO)

    event_type = evento["eventType"]
    state = evento["state"]

    if event_type == "imobilidade.leitura":
        # Heartbeat: so atualiza o "ultimo estado conhecido da maquina",
        # nao produz atuacao por si so.
        estado.estado_atual = state
        return None

    # event_type == "imobilidade.decisao"
    estado.ultima_decisao = state

    if state in ESTADOS_QUE_ENCERRAM_ALERTA:
        estado.alerta_ativo = False
        return None

    if state != "ALERTA_IMOBILIDADE":
        return None

    if estado.alerta_ativo:
        # Mesmo episodio ja alertado (nao deveria acontecer, ja que o
        # firmware so emite ALERTA_IMOBILIDADE uma vez por episodio via
        # sua propria maquina de estados - mas o servidor nao confia
        # cegamente no produtor: rearma so quando um dos estados de
        # ESTADOS_QUE_ENCERRAM_ALERTA chegar).
        return None

    estado.alerta_ativo = True
    return {
        "tipo": "alerta_imobilidade",
        "device_id": device_id,
        "entity_id": evento.get("entityId"),
        "event_time_ms": evento.get("eventTimeMs"),
        "sequence": evento.get("sequence"),
        "value": evento.get("value"),
        "unit": evento.get("unit"),
        "mensagem": (
            f"Imobilidade confirmada para '{evento.get('entityId', device_id)}' "
            f"(magnitude={evento.get('value')} {evento.get('unit', 'm/s2')}, "
            "sem confirmacao 'estou bem' dentro do tempo de espera)."
        ),
    }


def obter_estado(device_id: str) -> dict | None:
    """Procura o device_id em todos os usuarios. Exposto via GET /estado/<device_id> em server.py - e a evidencia
    observavel de 'mudanca de estado' pedida no roteiro da apresentacao,
    em complemento ao log em dataUsers/usuario_<id>.jsonl."""
    candidatos = [(uid, e) for (uid, dev), e in _estados.items() if dev == device_id]
    if not candidatos:
        return None
    # O mesmo device_id pode existir em mais de um usuario: devolve o do usuario atualizado por ultimo.
    user_id, estado = max(candidatos, key=lambda c: c[1].ultima_atualizacao or "")
    return {
        "device_id": device_id,
        "user_id": user_id,
        "estado_atual": estado.estado_atual,
        "ultima_decisao": estado.ultima_decisao,
        "alerta_ativo": estado.alerta_ativo,
        "ultimo_sequence": estado.ultimo_sequence,
        "ultima_atualizacao": estado.ultima_atualizacao,
        "ultimo_schema_version": estado.ultimo_schema_version,
    }
