"""
Contrato entre a borda (Servidor 1) e a analise (Servidor 2): os tipos que entram e saem e o seu JSON.

Entram: `Leitura` e `Resposta` (o que a borda aceitou). Saem: `Decisoes` (comandos de push, alertas, registros de
confirmacao, resumos de estado e descartes). Tudo e dado puro, sem I/O. A analise monta as `Decisoes`; a borda as grava e
entrega. Cada tipo tem `para_json` e a funcao `de_json_*` correspondente; instantes viajam em ISO 8601 com fuso.

Modulo de `comum/`: nao importa borda, analise nem Flask.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone


# ============================================================================
# Instantes
# ============================================================================
def exigir_utc(instante: datetime) -> datetime:
    if not isinstance(instante, datetime) or instante.tzinfo is None:
        raise ValueError("instante deve ser um datetime com fuso (UTC)")
    return instante


def instante_para_json(instante: datetime | None) -> str | None:
    return None if instante is None else instante.astimezone(timezone.utc).isoformat()


def instante_de_json(valor, obrigatorio: bool = True) -> datetime | None:
    if valor is None and not obrigatorio:
        return None
    if not isinstance(valor, str):
        raise ValueError("instante ausente ou nao e texto")
    try:
        return exigir_utc(datetime.fromisoformat(valor))
    except (TypeError, ValueError):
        raise ValueError("instante invalido (esperado ISO 8601 com fuso)") from None


# ============================================================================
# Entrada
# ============================================================================
@dataclass(frozen=True)
class Leitura:
    """Uma leitura aceita pela borda. `payload` sao os dados da fonte SEM `source` e `user_id`; `schema_version` e a versao
    ja verificada."""
    instante: datetime          # instante da ingestao (UTC)
    user_id: int
    device_id: str
    fonte: str                  # "app" | "wokwi"
    payload: dict
    schema_version: int

    def __post_init__(self):
        exigir_utc(self.instante)

    def para_json(self) -> dict:
        return {"tipo": "leitura", "instante": instante_para_json(self.instante), "user_id": self.user_id,
                "device_id": self.device_id, "fonte": self.fonte, "payload": self.payload,
                "schema_version": self.schema_version}


@dataclass(frozen=True)
class Resposta:
    """Resposta de um dispositivo: o evento JA normalizado por `borda.validacao.validar_evento_retorno`
    ({eventType, state, messageId, device_id, source}). A identidade vem do token, nunca do corpo."""
    instante: datetime          # instante da ingestao (UTC)
    user_id: int
    device_id: str | None
    fonte: str
    evento: dict
    schema_version: int

    def __post_init__(self):
        exigir_utc(self.instante)

    def para_json(self) -> dict:
        return {"tipo": "resposta", "instante": instante_para_json(self.instante), "user_id": self.user_id,
                "device_id": self.device_id, "fonte": self.fonte, "evento": self.evento,
                "schema_version": self.schema_version}


def _inteiro(valor) -> bool:
    return isinstance(valor, int) and not isinstance(valor, bool)


def _conferir_comum(d: dict, fontes: set[str]) -> None:
    if not isinstance(d, dict):
        raise ValueError("item nao e um objeto")
    if not _inteiro(d.get("user_id")):
        raise ValueError("user_id ausente ou nao inteiro")
    if d.get("fonte") not in fontes:
        raise ValueError("fonte desconhecida")
    if not _inteiro(d.get("schema_version")):
        raise ValueError("schema_version ausente ou nao inteiro")


def de_json_item(d: dict, fontes: set[str] = frozenset({"app", "wokwi"})) -> "Leitura | Resposta":
    """JSON de um item de entrada -> `Leitura` ou `Resposta`. Levanta ValueError (sem o conteudo do item na mensagem)."""
    _conferir_comum(d, set(fontes))
    instante = instante_de_json(d.get("instante"))
    if d.get("tipo") == "leitura":
        if not isinstance(d.get("device_id"), str) or not d["device_id"]:
            raise ValueError("device_id ausente")
        if not isinstance(d.get("payload"), dict):
            raise ValueError("payload nao e um objeto")
        return Leitura(instante, d["user_id"], d["device_id"], d["fonte"], d["payload"], d["schema_version"])
    if d.get("tipo") == "resposta":
        if d.get("device_id") is not None and not isinstance(d["device_id"], str):
            raise ValueError("device_id nao e texto")
        if not isinstance(d.get("evento"), dict):
            raise ValueError("evento nao e um objeto")
        return Resposta(instante, d["user_id"], d.get("device_id"), d["fonte"], d["evento"], d["schema_version"])
    raise ValueError("tipo de item desconhecido")


# ============================================================================
# Saida
# ============================================================================
@dataclass(frozen=True)
class MudancaEpisodio:
    """Estado novo de um episodio (linha de `episodios`). `tipo` e o motivo na abertura e "DESCONHECIDO" no
    encerramento (o repositorio nao sobrescreve o tipo de um episodio que ja existe)."""
    episodio_id: str
    user_id: int
    device_id: str | None
    tipo: str
    estado: str                 # "aguardando_confirmacao" | "encerrado"
    aberto_em: datetime
    atualizado_em: datetime
    encerrado_em: datetime | None
    motivo_encerramento: str | None
    dados: dict

    def como_linha(self) -> dict:
        return {"episodio_id": self.episodio_id, "user_id": self.user_id, "device_id": self.device_id,
                "tipo": self.tipo, "estado": self.estado, "aberto_em": self.aberto_em,
                "atualizado_em": self.atualizado_em, "encerrado_em": self.encerrado_em,
                "motivo_encerramento": self.motivo_encerramento, "dados": dict(self.dados)}

    def para_json(self) -> dict:
        d = self.como_linha()
        for k in ("aberto_em", "atualizado_em", "encerrado_em"):
            d[k] = instante_para_json(d[k])
        return d

    @classmethod
    def de_json(cls, d: dict) -> "MudancaEpisodio":
        return cls(d["episodio_id"], d["user_id"], d.get("device_id"), d["tipo"], d["estado"],
                   instante_de_json(d["aberto_em"]), instante_de_json(d["atualizado_em"]),
                   instante_de_json(d.get("encerrado_em"), obrigatorio=False), d.get("motivo_encerramento"),
                   dict(d.get("dados") or {}))


@dataclass(frozen=True)
class Comando:
    """Mensagem de push a entregar (linha de `comandos_push`), com a mudanca de episodio que vai na MESMA transacao.
    O `message_id` e gerado pela analise."""
    user_id: int
    device_id: str | None       # dispositivo que originou o episodio (informativo)
    mensagem: dict              # contrato push-mensagem
    criado_em: datetime
    expira_em: datetime | None
    episodio: MudancaEpisodio

    def como_linha(self) -> dict:
        return {"criado_em": self.criado_em, "user_id": self.user_id, "device_id": None, "canal": "todos",
                "message_id": self.mensagem["messageId"], "tipo": self.mensagem["messageType"],
                "payload": self.mensagem, "expira_em": self.expira_em, "episodio_id": self.episodio.episodio_id}

    def para_json(self) -> dict:
        return {"user_id": self.user_id, "device_id": self.device_id, "mensagem": self.mensagem,
                "criado_em": instante_para_json(self.criado_em), "expira_em": instante_para_json(self.expira_em),
                "episodio": self.episodio.para_json()}

    @classmethod
    def de_json(cls, d: dict) -> "Comando":
        return cls(d["user_id"], d.get("device_id"), d["mensagem"], instante_de_json(d["criado_em"]),
                   instante_de_json(d.get("expira_em"), obrigatorio=False), MudancaEpisodio.de_json(d["episodio"]))


@dataclass(frozen=True)
class Alerta:
    """Alerta de regra (linha de `alertas`): `tipo` e "luz_inadequada" ou "imobilidade"."""
    user_id: int
    device_id: str
    tipo: str
    dados: dict                 # o dicionario devolvido pela regra
    schema_version: int
    instante: datetime

    def como_linha(self) -> dict:
        return {"instante": self.instante, "user_id": self.user_id, "device_id": self.device_id, "tipo": self.tipo,
                "mensagem": self.dados.get("mensagem"), "dados": self.dados, "schema_version": self.schema_version}

    def para_json(self) -> dict:
        return {"user_id": self.user_id, "device_id": self.device_id, "tipo": self.tipo, "dados": self.dados,
                "schema_version": self.schema_version, "instante": instante_para_json(self.instante)}

    @classmethod
    def de_json(cls, d: dict) -> "Alerta":
        return cls(d["user_id"], d["device_id"], d["tipo"], d["dados"], d["schema_version"],
                   instante_de_json(d["instante"]))


@dataclass(frozen=True)
class Confirmacao:
    """Registro do ciclo de confirmacao (episodio.aberto, push.ack, confirmacao.resposta, episodio.encerrado...)."""
    user_id: int
    tipo: str
    dados: dict
    instante: datetime

    def para_json(self) -> dict:
        return {"user_id": self.user_id, "tipo": self.tipo, "dados": self.dados,
                "instante": instante_para_json(self.instante)}

    @classmethod
    def de_json(cls, d: dict) -> "Confirmacao":
        return cls(d["user_id"], d["tipo"], d["dados"], instante_de_json(d["instante"]))


@dataclass(frozen=True)
class ResumoEstado:
    """Resumo do estado da regra de imobilidade de um dispositivo (linha de `estado_dispositivo`)."""
    user_id: int
    device_id: str
    instante: datetime
    versao_regra: int
    estado: dict

    @property
    def assinatura(self) -> tuple:
        """So o que justifica uma nova gravacao: quem aplica grava apenas quando isto muda."""
        return (self.estado.get("estado_atual"), self.estado.get("ultima_decisao"), self.estado.get("alerta_ativo"))

    def como_linha(self) -> dict:
        return {"user_id": self.user_id, "device_id": self.device_id, "atualizado_em": self.instante,
                "versao_regra": self.versao_regra, "estado": self.estado}

    def para_json(self) -> dict:
        return {"user_id": self.user_id, "device_id": self.device_id, "instante": instante_para_json(self.instante),
                "versao_regra": self.versao_regra, "estado": self.estado}

    @classmethod
    def de_json(cls, d: dict) -> "ResumoEstado":
        return cls(d["user_id"], d["device_id"], instante_de_json(d["instante"]), d["versao_regra"], d["estado"])


@dataclass(frozen=True)
class Descarte:
    """Leitura que nao chegou a alimentar uma regra (para log; nunca derruba nada)."""
    user_id: int
    device_id: str | None
    motivo: str
    detalhe: str = ""

    def para_json(self) -> dict:
        return {"user_id": self.user_id, "device_id": self.device_id, "motivo": self.motivo, "detalhe": self.detalhe}

    @classmethod
    def de_json(cls, d: dict) -> "Descarte":
        return cls(d["user_id"], d.get("device_id"), d["motivo"], d.get("detalhe", ""))


@dataclass
class Decisoes:
    comandos: list[Comando] = field(default_factory=list)
    alertas: list[Alerta] = field(default_factory=list)
    confirmacoes: list[Confirmacao] = field(default_factory=list)
    estados: list[ResumoEstado] = field(default_factory=list)
    descartes: list[Descarte] = field(default_factory=list)

    @property
    def episodios(self) -> list[MudancaEpisodio]:
        return [c.episodio for c in self.comandos]

    def somar(self, outra: "Decisoes") -> "Decisoes":
        self.comandos += outra.comandos
        self.alertas += outra.alertas
        self.confirmacoes += outra.confirmacoes
        self.estados += outra.estados
        self.descartes += outra.descartes
        return self

    def __bool__(self) -> bool:
        return bool(self.comandos or self.alertas or self.confirmacoes or self.estados or self.descartes)

    def para_json(self) -> dict:
        return {"comandos": [c.para_json() for c in self.comandos], "alertas": [a.para_json() for a in self.alertas],
                "confirmacoes": [c.para_json() for c in self.confirmacoes],
                "estados": [e.para_json() for e in self.estados], "descartes": [d.para_json() for d in self.descartes]}

    @classmethod
    def de_json(cls, d: dict) -> "Decisoes":
        return cls([Comando.de_json(x) for x in d.get("comandos", [])], [Alerta.de_json(x) for x in d.get("alertas", [])],
                   [Confirmacao.de_json(x) for x in d.get("confirmacoes", [])],
                   [ResumoEstado.de_json(x) for x in d.get("estados", [])],
                   [Descarte.de_json(x) for x in d.get("descartes", [])])
