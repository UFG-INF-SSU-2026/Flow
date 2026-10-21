"""
Nucleo das regras (roadmap do split, S2): "processar este lote com este relogio e este estado".

Entra: leituras e respostas de dispositivo, cada uma com o seu INSTANTE DE INGESTAO (o `instante_ingestao` que a
borda grava em `leituras` / `respostas_dispositivo`), e o `EstadoRegras` de quem esta processando.
Sai: `Decisoes` (dados puros): comandos de push (cada um com a mudanca de episodio que deve ser gravada junto),
alertas, registros de confirmacao, resumos de estado e descartes.

O que o nucleo NAO faz (e por que ele serve ao servidor de hoje, a sombra da S3, a analise da S4 e as simulacoes):
  - nao le o relogio: o instante vem no argumento. O vigia (`vigiar`) tambem recebe o relogio de quem o chama,
    porque so ele depende do tempo que passa e nao do tempo dos dados;
  - nao tem thread, nao toca arquivo nem banco, nao envia nada. Quem grava e `analise/efeitos.py` e quem entrega
    e o ADAPTADOR (hoje, borda/caminho_direto.py; na S4, o laco da analise e a entrega de push da borda);
  - nao guarda estado global: o estado e o `EstadoRegras` recebido. `PADRAO` aponta para as tabelas dos modulos
    de regra (o estado do servidor); `novo_estado()` da um estado limpo e independente.

Ordem das decisoes: para o mesmo instante, quem aplica deve aplicar `confirmacoes`, depois `alertas`/`estados`, depois
`comandos` (cada um grava episodio + comando juntos e entao entrega). E a mesma ordem em que os efeitos aconteciam
antes da extracao.

Validacao: a estrutura ja foi conferida pela borda para aceitar o dado, mas a analise precisa tratar do mesmo jeito
o que ja esta no banco (leitura estruturalmente invalida e gravada e depois descartada, como hoje). Por isso o
nucleo repete a conferencia estrutural, chamando o MESMO validador e os MESMOS schemas da borda
(`comum.contratos.validar`, contratos de schemas/), sem copia de regra. A analise nao importa a borda: o que as duas
partilham mora em `comum/`. Quando a borda passar a recusar com 400, essa chamada sai daqui.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from comum import const as const_comum
from comum import contratos, mensagens
from analise import regra_imobilidade
from analise import regra_luz
from analise import regra_queda

# Resultado da cascata de validacao de um evento wokwi (so ACEITO alimenta a regra de queda).
WOKWI_INVALIDO = "INVALIDO"
WOKWI_DUPLICADO = "DUPLICADO"
WOKWI_ACEITO = "ACEITO"

# Motivos de descarte (contados nas metricas da analise)
DESCARTE_ESTRUTURA = "estrutura_invalida"
DESCARTE_SEMANTICA = "semantica_invalida"
DESCARTE_DUPLICADA = "duplicada"
DESCARTE_FALHA = "falha_na_regra"


# ============================================================================
# Entrada
# ============================================================================
def _exigir_utc(instante: datetime) -> datetime:
    if not isinstance(instante, datetime) or instante.tzinfo is None:
        raise ValueError("instante deve ser um datetime com fuso (UTC)")
    return instante


@dataclass(frozen=True)
class Leitura:
    """Uma leitura aceita pela borda (a linha de `leituras`).

    `payload` e o que foi gravado: os dados da fonte SEM `source` e `user_id`. `schema_version` e a versao ja
    verificada (coluna `schema_version`); o nucleo a coloca no `schemaVersion` que as regras enxergam, como
    server.py sempre fez."""
    instante: datetime          # instante_ingestao (UTC)
    user_id: int
    device_id: str
    fonte: str                  # "app" | "wokwi"
    payload: dict
    schema_version: int

    def __post_init__(self):
        _exigir_utc(self.instante)


@dataclass(frozen=True)
class Resposta:
    """Uma resposta de dispositivo (a linha de `respostas_dispositivo`): o evento JA normalizado por
    `borda.validacao.validar_evento_retorno` ({eventType, state, messageId, device_id, source}). A identidade
    (`user_id`, `device_id`) vem do token, nunca do corpo (DS1)."""
    instante: datetime          # instante_ingestao (UTC)
    user_id: int
    device_id: str | None
    fonte: str
    evento: dict
    schema_version: int

    def __post_init__(self):
        _exigir_utc(self.instante)


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


@dataclass(frozen=True)
class Comando:
    """Mensagem de push a entregar (linha de `comandos_push`), com a mudanca de episodio que vai na MESMA transacao
    (DS5/DS6). O `message_id` e gerado aqui, pelo nucleo (DS2)."""
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


@dataclass(frozen=True)
class Confirmacao:
    """Registro do ciclo de confirmacao (episodio.aberto, push.ack, confirmacao.resposta, episodio.encerrado...)."""
    user_id: int
    tipo: str
    dados: dict
    instante: datetime

    def aplicar(self) -> None:
        """Grava como `regra_queda.registrar` sempre gravou (arquivo + ouvinte do banco), com o instante da decisao."""
        regra_queda.registrar(self.user_id, self.tipo, self.dados, self.instante.timestamp())


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


@dataclass(frozen=True)
class Descarte:
    """Leitura que nao chegou a alimentar uma regra (para metricas e log; nunca derruba o laco)."""
    user_id: int
    device_id: str | None
    motivo: str
    detalhe: str = ""


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


# ============================================================================
# Estado
# ============================================================================
@dataclass
class EstadoRegras:
    """O estado em memoria das tres regras. Cada `EstadoRegras` e um mundo a parte."""
    queda: regra_queda.EstadoQueda = field(default_factory=regra_queda.EstadoQueda)
    imobilidade: regra_imobilidade.EstadoImobilidade = field(default_factory=regra_imobilidade.EstadoImobilidade)
    luz: regra_luz.EstadoLuz = field(default_factory=regra_luz.EstadoLuz)


def novo_estado() -> EstadoRegras:
    """Estado limpo e independente (sombra, simulacao, teste)."""
    return EstadoRegras()


# O estado do servidor de hoje: as tabelas dos proprios modulos de regra (quem chama os modulos direto, como os
# testes legados, enxerga as mesmas tabelas).
PADRAO = EstadoRegras(regra_queda.PADRAO, regra_imobilidade.PADRAO, regra_luz.PADRAO)


# ============================================================================
# Passos
# ============================================================================
def _epoch(instante: datetime) -> float:
    return instante.timestamp()


def comando_de_pedido(estado: EstadoRegras, user_id: int, pedido: "regra_queda.PedidoConfirmacao",
                      device_id: str | None, instante: datetime) -> Comando:
    """Monta o comando de push de um pedido de confirmacao e anota a `messageId` no mapa do episodio (e por ele
    que a resposta do dispositivo sera correlacionada)."""
    msg = mensagens.criar_pedido_confirmacao(user_id, pedido.episode_id, pedido.timeout_s, pedido.motivo,
                                             agora=instante)
    regra_queda.anotar_mensagem(user_id, pedido.episode_id, msg["messageId"], agora=_epoch(instante),
                                estado=estado.queda)
    episodio = MudancaEpisodio(pedido.episode_id, user_id, device_id, pedido.motivo, "aguardando_confirmacao",
                               instante, instante, None, None, {"timeout_s": pedido.timeout_s})
    expira = instante + timedelta(seconds=pedido.timeout_s + const_comum.MARGEM_SERVIDOR_S)
    return Comando(user_id, device_id, msg, instante, expira, episodio)


def comando_de_encerramento(enc: "regra_queda.Encerramento", instante: datetime) -> Comando:
    """Monta o comando `queda.confirmacao_encerrada` e a mudanca do episodio para `encerrado`."""
    msg = mensagens.criar_encerramento(enc.user_id, enc.episode_id, enc.resultado, agora=instante)
    episodio = MudancaEpisodio(enc.episode_id, enc.user_id, None, "DESCONHECIDO", "encerrado",
                               instante, instante, instante, enc.resultado, {})
    return Comando(enc.user_id, None, msg, instante, None, episodio)


def _registros(anotados: list, d: Decisoes) -> None:
    for r in anotados:
        d.confirmacoes.append(Confirmacao(r.user_id, r.tipo, r.dados,
                                          datetime.fromtimestamp(r.instante, tz=timezone.utc)))


def _falha_na_regra(nome: str, e: Exception, leitura: Leitura, d: Decisoes) -> None:
    """Falha inesperada numa regra (payload malformado ou bug): so loga e conta. Nunca derruba o laco."""
    print(f"  [{nome}] falha no fluxo legado (ignorada): {type(e).__name__}: {str(e)[:200]}")
    d.descartes.append(Descarte(leitura.user_id, leitura.device_id, DESCARTE_FALHA, nome))


def _regra_de_luz(estado: EstadoRegras, leitura: Leitura, payload: dict, d: Decisoes) -> None:
    """Fluxo priorizado: LeituraAmbiente -> validacao -> deduplicacao -> regra de CCT -> alerta (so o app emite)."""
    bloco = payload.get("leitura_ambiente")
    if bloco is None:
        return
    erros = contratos.validar("app-leitura-ambiente", bloco)
    if erros:
        print(f"  [leitura_ambiente] descartada (estrutura invalida): {'; '.join(erros[:3])}")
        d.descartes.append(Descarte(leitura.user_id, leitura.device_id, DESCARTE_ESTRUTURA, "leitura_ambiente"))
        return
    try:
        regra_luz.validar_evento(bloco)
    except regra_luz.EventoInvalido as e:
        print(f"  [leitura_ambiente] descartada (invalida): {e}")
        d.descartes.append(Descarte(leitura.user_id, leitura.device_id, DESCARTE_SEMANTICA, "leitura_ambiente"))
        return
    if regra_luz.eh_duplicado(leitura.user_id, bloco, estado=estado.luz):
        print(f"  [leitura_ambiente] descartada (duplicada, seq_num={bloco['seq_num']})")
        d.descartes.append(Descarte(leitura.user_id, leitura.device_id, DESCARTE_DUPLICADA, "leitura_ambiente"))
        return
    alerta = regra_luz.avaliar_regra(leitura.user_id, bloco, estado=estado.luz)
    if alerta is not None:
        d.alertas.append(Alerta(leitura.user_id, leitura.device_id, "luz_inadequada", alerta,
                                const_comum.SCHEMA_VERSION_APP, leitura.instante))


def _regra_de_imobilidade(estado: EstadoRegras, leitura: Leitura, payload: dict, d: Decisoes) -> tuple[str, dict | None]:
    """Cascata de validacao por eventType + tabela de estado + atuacao para ALERTA_IMOBILIDADE.
    Devolve (INVALIDO | DUPLICADO | ACEITO, alerta ou None): so ACEITO alimenta a regra de queda."""
    erros = contratos.validar("wokwi-telemetria", payload)
    if erros:
        print(f"  [wokwi] evento descartado (estrutura invalida): {'; '.join(erros[:3])}")
        d.descartes.append(Descarte(leitura.user_id, leitura.device_id, DESCARTE_ESTRUTURA, "wokwi"))
        return WOKWI_INVALIDO, None
    try:
        regra_imobilidade.validar_evento(payload)
    except regra_imobilidade.EventoInvalido as e:
        print(f"  [wokwi] evento descartado (invalido): {e}")
        d.descartes.append(Descarte(leitura.user_id, leitura.device_id, DESCARTE_SEMANTICA, "wokwi"))
        return WOKWI_INVALIDO, None
    if regra_imobilidade.eh_duplicado(leitura.user_id, payload, estado=estado.imobilidade):
        print(f"  [wokwi] evento descartado (duplicado/atrasado, sequence={payload.get('sequence')})")
        d.descartes.append(Descarte(leitura.user_id, leitura.device_id, DESCARTE_DUPLICADA, "wokwi"))
        return WOKWI_DUPLICADO, None
    alerta = regra_imobilidade.processar_evento(leitura.user_id, payload, agora=_epoch(leitura.instante),
                                                estado=estado.imobilidade)
    if alerta is not None:
        d.alertas.append(Alerta(leitura.user_id, leitura.device_id, "imobilidade", alerta,
                                const_comum.SCHEMA_VERSION_WOKWI, leitura.instante))
    return WOKWI_ACEITO, alerta


def _resumo_de_estado(estado: EstadoRegras, leitura: Leitura, d: Decisoes) -> None:
    """Resumo do estado de imobilidade do dispositivo (DS3). Nunca levanta: um resumo perdido nao vale uma leitura."""
    try:
        resumo = regra_imobilidade.obter_estado(leitura.device_id, estado=estado.imobilidade)
        if resumo is None or resumo.get("user_id") != leitura.user_id:
            return
        d.estados.append(ResumoEstado(leitura.user_id, leitura.device_id, leitura.instante,
                                      resumo.get("ultimo_schema_version") or 1, resumo))
    except Exception as e:  # noqa: BLE001
        print(f"  [db] falha ao preparar estado_dispositivo user={leitura.user_id}: {type(e).__name__}: {str(e)[:200]}")


def _regra_de_queda(estado: EstadoRegras, leitura: Leitura, payload: dict, alerta_imobilidade: dict | None,
                    d: Decisoes) -> None:
    """Gatilho A (impacto + imobilidade) tem preferencia; o gatilho B usa o alerta da regra de imobilidade."""
    agora = _epoch(leitura.instante)
    pedido = regra_queda.avaliar(leitura.user_id, leitura.fonte, payload, agora=agora,
                                 device_id=leitura.device_id, estado=estado.queda)
    if pedido is None and alerta_imobilidade is not None:
        pedido = regra_queda.abrir_por_imobilidade(leitura.user_id, agora=agora, estado=estado.queda)
    if pedido is not None:
        d.comandos.append(comando_de_pedido(estado, leitura.user_id, pedido, leitura.device_id, leitura.instante))


def processar_leitura(estado: EstadoRegras, leitura: Leitura) -> Decisoes:
    """Uma leitura aceita pela borda -> decisoes. O relogio das regras e `leitura.instante`."""
    d = Decisoes()
    payload = {**leitura.payload, "schemaVersion": leitura.schema_version}
    with regra_queda.coletar_registros() as anotados:
        try:
            alimenta_queda = True      # validar -> deduplicar -> avaliar: invalido ou duplicado nao chega a regra de queda
            alerta_imobilidade = None
            if leitura.fonte == "app":
                # A analise de queda NAO depende do fluxo legado do app: se ele falhar, loga e segue.
                try:
                    _regra_de_luz(estado, leitura, payload, d)
                except Exception as e:  # noqa: BLE001
                    _falha_na_regra("leitura_ambiente", e, leitura, d)
                alimenta_queda = not regra_queda.eh_duplicado_app(leitura.user_id, leitura.device_id, payload,
                                                                  estado=estado.queda)
            elif leitura.fonte == "wokwi":
                # Para o wokwi a queda depende so do estado ACEITO: excecao no fluxo legado = nao aceito.
                try:
                    resultado, alerta_imobilidade = _regra_de_imobilidade(estado, leitura, payload, d)
                except Exception as e:  # noqa: BLE001
                    _falha_na_regra("wokwi", e, leitura, d)
                    resultado, alerta_imobilidade = WOKWI_INVALIDO, None
                alimenta_queda = resultado == WOKWI_ACEITO
                if alimenta_queda:
                    _resumo_de_estado(estado, leitura, d)
            if alimenta_queda:
                try:
                    _regra_de_queda(estado, leitura, payload, alerta_imobilidade, d)
                except Exception as e:  # noqa: BLE001
                    print(f"  [queda] falha ao avaliar ou emitir o pedido user={leitura.user_id}: "
                          f"{type(e).__name__}: {str(e)[:200]}")
                    d.descartes.append(Descarte(leitura.user_id, leitura.device_id, DESCARTE_FALHA, "queda"))
        finally:
            _registros(anotados, d)
    return d


def processar_resposta(estado: EstadoRegras, resposta: Resposta) -> Decisoes:
    """Resposta de dispositivo (push.ack, confirmacao.resposta) -> decisoes. Correlaciona por `messageId` e, se for
    ESTOU_BEM de um episodio aberto, encerra como CONFIRMADO. A resposta de outro usuario nao encerra nada (DS1)."""
    d = Decisoes()
    with regra_queda.coletar_registros() as anotados:
        try:
            enc = regra_queda.registrar_retorno(resposta.user_id, resposta.evento, agora=_epoch(resposta.instante),
                                                estado=estado.queda)
        finally:
            _registros(anotados, d)
    if enc is not None:
        d.comandos.append(comando_de_encerramento(enc, resposta.instante))
    return d


def vigiar(estado: EstadoRegras, instante: datetime) -> Decisoes:
    """Passo SEPARADO do laco: encerra como EXPIRADO os episodios com prazo vencido em `instante`. E o unico passo
    cujo relogio e o do mundo real: quem chama passa `tempo.agora_utc()`."""
    _exigir_utc(instante)
    d = Decisoes()
    with regra_queda.coletar_registros() as anotados:
        try:
            encerramentos = regra_queda.vigiar(agora=_epoch(instante), estado=estado.queda)
        finally:
            _registros(anotados, d)
    for enc in encerramentos:
        d.comandos.append(comando_de_encerramento(enc, instante))
    return d


def processar_lote(estado: EstadoRegras, itens: list) -> Decisoes:
    """Leituras e respostas (`Leitura` | `Resposta`) em ordem de ingestao -> decisoes de todas. A ordenacao por
    instante e estavel: itens do mesmo instante mantem a ordem recebida. Sem vigia: chame `vigiar` entre os lotes."""
    for item in itens:      # confere os tipos ANTES de ordenar: um item estranho nao pode ter efeito parcial
        if not isinstance(item, (Leitura, Resposta)):
            raise TypeError(f"item de lote desconhecido: {type(item).__name__}")
    total = Decisoes()
    for item in sorted(itens, key=lambda i: i.instante):
        if isinstance(item, Leitura):
            total.somar(processar_leitura(estado, item))
        else:
            total.somar(processar_resposta(estado, item))
    return total