"""
Regra de negocio: decisao de imobilidade do wokwi (cascata de validacao,
tabela de estado por dispositivo e atuacao para ALERTA_IMOBILIDADE).

Fluxo: evento wokwi (imobilidade.leitura | imobilidade.decisao) ->
validacao estrutural (borda) -> validacao semantica -> deduplicacao -> atualizacao de estado persistente
-> atuacao (quando state == "ALERTA_IMOBILIDADE").

Contrato do evento (emitido por sketch/sketch.ino, funcao emitirEvento):
    schemaVersion, eventType, deviceId, entityId, eventTimeMs, sequence, value, unit, state

Este modulo espelha deliberadamente a estrutura de regra_luz.py (mesma
separacao validar_evento / eh_duplicado / avaliar-e-atuar), para que as
duas regras do servidor sigam o mesmo padrao e fiquem faceis de explicar
lado a lado na verificacao oral.

RELOGIO E ESTADO EXPLICITOS (S2a do split): `processar_evento` recebe o instante (`agora`, segundos desde a epoca)
em vez de ler o relogio, e o estado mora num `EstadoImobilidade` (parametro `estado=`; sem ele vale o `PADRAO` do
modulo, de que `_estados` e apelido). O nucleo (nucleo.py) sempre passa os dois.
"""

import itertools
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from analise import const as const_analise   # ACEL_MIN/MAX_PLAUSIVEL, MAX_ESTADOS_LEGADOS
from comum import const as const_comum       # SCHEMA_VERSION_WOKWI/PADRAO

# Versao do schema do payload wokwi (docs/arquitetura.md, secao 4.3).
# O produtor atual (sketch.ino) envia a versao do contrato do wokwi (const_comum.SCHEMA_VERSION_WOKWI).
# Eventos sem o campo (produtor mais antigo/nao atualizado) sao tratados como
# versao 1 (const_comum.SCHEMA_VERSION_PADRAO) - nao sao rejeitados, apenas logados
# como desatualizados (ver validar_evento).

# O vocabulario (eventType -> state) e os campos obrigatorios vivem em schemas/wokwi-telemetria.json e sao
# conferidos pela borda (validacao.erros_telemetria_wokwi). Aqui ficam so as regras de negocio.

# States de decisao que, ao chegar, encerram um episodio de alerta em aberto
# (o dispositivo voltou a um estado seguro) - usados para "rearmar" o flag
# alerta_ativo do lado do servidor, simetrico ao alerta_ativo do firmware.
ESTADOS_QUE_ENCERRAM_ALERTA = {"CONFIRMADO_OK", "MOVIMENTO_RETOMADO", "REARMADO_MANUAL"}


class EventoInvalido(Exception):
    """Evento fora da faixa fisica plausivel (a estrutura invalida e barrada antes, na borda)."""


@dataclass
class EstadoDispositivo:
    """Estado mantido em memoria por deviceId - ao contrario do buffer de
    agregacao de server.py (que e limpo a cada flush de 1s), esta tabela
    persiste entre janelas e guarda a "mudanca de estado" do dispositivo."""
    ultimo_sequence: int | None = None
    estado_atual: str = "DESCONHECIDO"      # ultimo state de imobilidade.leitura (heartbeat)
    ultima_decisao: str | None = None       # ultimo state de imobilidade.decisao
    alerta_ativo: bool = False              # evita reemitir arquivo de alerta para o mesmo episodio
    ultima_atualizacao: str | None = None   # ISO 8601, para saber ha quanto tempo o device fala com o servidor
    ultimo_schema_version: int | None = None  # ultimo schemaVersion recebido (ou const_comum.SCHEMA_VERSION_PADRAO, se ausente)
    ordem_atualizacao: int = 0              # contador monotonico: desempata o "atualizado por ultimo" (o relogio do Windows
                                            # tem ~15 ms de resolucao e duas atualizacoes seguidas podem ter o mesmo ISO)


# Estado por (user_id, device_id) (em memoria; reinicia se o servidor reiniciar,
# mesma decisao de simplicidade ja assumida em regra_luz.py). O user_id faz parte da chave: dois usuarios com o
# mesmo device_id nao compartilham estado nem sequence.
# Como o device_id vem do cliente, a tabela tem teto (const_analise.MAX_ESTADOS_LEGADOS): estoura, sai o menos usado
# (a ordem do dict e a do ultimo uso).
@dataclass
class EstadoImobilidade:
    """A tabela de estados da regra (uma instancia por 'mundo': laco da analise, simulacao, teste)."""
    estados: dict[tuple[int, str], EstadoDispositivo] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    contador: "itertools.count" = field(default_factory=lambda: itertools.count(1), repr=False)


PADRAO = EstadoImobilidade()
_estados = PADRAO.estados     # apelido da tabela do PADRAO (limpar uma limpa a outra)
_estados_lock = PADRAO.lock
_contador_atualizacoes = PADRAO.contador


def _tabela(estado: "EstadoImobilidade | None") -> EstadoImobilidade:
    return PADRAO if estado is None else estado


def _estado_do(user_id: int, device_id: str, estado: "EstadoImobilidade | None" = None) -> EstadoDispositivo:
    tab = _tabela(estado)
    chave = (user_id, device_id)
    with tab.lock:
        atual = tab.estados.pop(chave, None) or EstadoDispositivo()
        tab.estados[chave] = atual   # reinsere no fim: o primeiro do dict e sempre o menos usado
        while len(tab.estados) > const_analise.MAX_ESTADOS_LEGADOS:
            del tab.estados[next(iter(tab.estados))]
        return atual


def _agora_iso() -> str:
    """Padrao para quem NAO passa `agora` (testes, chamadas avulsas). O nucleo sempre passa o instante da leitura."""
    return datetime.now(timezone.utc).isoformat()


def validar_evento(evento: dict) -> None:
    """Validacao SEMANTICA (a estrutura -- campos, tipos, vocabulario -- ja foi conferida pela borda):

        0. schemaVersion (apenas identificacao/aviso, nao rejeita)
        1. value dentro da faixa fisica plausivel (0-160 m/s2)

    A faixa fisica e verificada de forma coerente com o que o proprio firmware envia: LEITURA_INVALIDA usa o
    sentinela -1.0 (leitura falhou, nao ha magnitude) e LEITURA_FORA_DE_FAIXA reporta magnitude real acima do teto
    (>160) - validar esses dois contra 0-160 rejeitaria exatamente os eventos que existem para *avisar* sobre a
    anomalia. Levanta EventoInvalido se falhar.
    """
    schema_version = evento.get("schemaVersion", const_comum.SCHEMA_VERSION_PADRAO)
    if isinstance(schema_version, int) and schema_version < const_comum.SCHEMA_VERSION_WOKWI:
        print(
            f"  [wokwi] aviso: evento com schemaVersion={schema_version} "
            f"desatualizado (atual={const_comum.SCHEMA_VERSION_WOKWI}), "
            f"deviceId={evento.get('deviceId')}"
        )

    state = evento["state"]
    value = evento["value"]
    if state == "LEITURA_INVALIDA":
        if value != -1.0:
            raise EventoInvalido(
                f"LEITURA_INVALIDA deveria vir com o sentinela -1.0, veio {value}"
            )
    elif state == "LEITURA_FORA_DE_FAIXA":
        if value <= const_analise.ACEL_MAX_PLAUSIVEL:
            raise EventoInvalido(
                f"LEITURA_FORA_DE_FAIXA deveria vir com value > {const_analise.ACEL_MAX_PLAUSIVEL}, veio {value}"
            )
    else:
        if not (const_analise.ACEL_MIN_PLAUSIVEL <= value <= const_analise.ACEL_MAX_PLAUSIVEL):
            raise EventoInvalido(
                f"value fora da faixa fisica plausivel "
                f"({const_analise.ACEL_MIN_PLAUSIVEL}-{const_analise.ACEL_MAX_PLAUSIVEL}): {value}"
            )


def eh_duplicado(user_id: int, evento: dict, estado: EstadoImobilidade | None = None) -> bool:
    """Deduplicacao/idempotencia: sequence deve ser maior que o ultimo
    conhecido para aquele (user_id, deviceId) (sequence e global por dispositivo no
    firmware, nao por eventType - um unico contador incrementa a cada
    emitirEvento(), seja leitura ou decisao)."""
    device_id = str(evento["deviceId"])
    do_device = _estado_do(user_id, device_id, estado)
    seq = evento["sequence"]

    if do_device.ultimo_sequence is not None and seq <= do_device.ultimo_sequence:
        return True

    do_device.ultimo_sequence = seq
    return False


def processar_evento(user_id: int, evento: dict, agora: float | None = None,
                     estado: EstadoImobilidade | None = None) -> dict | None:
    """Atualiza a tabela de estado persistente e, quando aplicavel, produz
    a atuacao observavel (alerta) para state == "ALERTA_IMOBILIDADE".

    Retorna um dicionario de alerta se uma nova atuacao deve ser
    registrada nesta chamada, ou None caso contrario (inclui os casos
    "e so o heartbeat", "decisao nao critica" e "alerta ja estava ativo
    para este episodio").

    `agora` e o instante da ingestao do evento (segundos desde a epoca); vira `ultima_atualizacao`.
    """
    tab = _tabela(estado)
    device_id = str(evento["deviceId"])
    do_device = _estado_do(user_id, device_id, tab)
    do_device.ultima_atualizacao = (_agora_iso() if agora is None
                                    else datetime.fromtimestamp(agora, tz=timezone.utc).isoformat())
    do_device.ordem_atualizacao = next(tab.contador)
    do_device.ultimo_schema_version = evento.get("schemaVersion", const_comum.SCHEMA_VERSION_PADRAO)

    event_type = evento["eventType"]
    state = evento["state"]

    if event_type == "imobilidade.leitura":
        # Heartbeat: so atualiza o "ultimo estado conhecido da maquina",
        # nao produz atuacao por si so.
        do_device.estado_atual = state
        return None

    # event_type == "imobilidade.decisao"
    do_device.ultima_decisao = state

    if state in ESTADOS_QUE_ENCERRAM_ALERTA:
        do_device.alerta_ativo = False
        return None

    if state != "ALERTA_IMOBILIDADE":
        return None

    if do_device.alerta_ativo:
        # Mesmo episodio ja alertado (nao deveria acontecer, ja que o
        # firmware so emite ALERTA_IMOBILIDADE uma vez por episodio via
        # sua propria maquina de estados - mas o servidor nao confia
        # cegamente no produtor: rearma so quando um dos estados de
        # ESTADOS_QUE_ENCERRAM_ALERTA chegar).
        return None

    do_device.alerta_ativo = True
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


def obter_estado(device_id: str, estado: EstadoImobilidade | None = None) -> dict | None:
    """Procura o device_id em todos os usuarios. O laco grava o resumo em `estado_dispositivo` e a borda o expoe em
    GET /estado/<device_id> - e a evidencia observavel de 'mudanca de estado', em complemento ao log em
    dataUsers/usuario_<id>.jsonl."""
    tab = _tabela(estado)
    with tab.lock:
        candidatos = [(uid, e) for (uid, dev), e in tab.estados.items() if dev == device_id]
    if not candidatos:
        return None
    # O mesmo device_id pode existir em mais de um usuario: devolve o do usuario atualizado por ultimo.
    user_id, do_device = max(candidatos, key=lambda c: c[1].ordem_atualizacao)
    return {
        "device_id": device_id,
        "user_id": user_id,
        "estado_atual": do_device.estado_atual,
        "ultima_decisao": do_device.ultima_decisao,
        "alerta_ativo": do_device.alerta_ativo,
        "ultimo_sequence": do_device.ultimo_sequence,
        "ultima_atualizacao": do_device.ultima_atualizacao,
        "ultimo_schema_version": do_device.ultimo_schema_version,
    }
