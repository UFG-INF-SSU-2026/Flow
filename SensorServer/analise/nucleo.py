"""
Nucleo das regras: "processar este lote com este relogio e este estado".

Entra: leituras e respostas de dispositivo, cada uma com o seu INSTANTE DE INGESTAO (o que a borda anotou ao aceitar o
dado), e o `EstadoRegras` de quem esta processando. Os tipos de entrada e saida moram em `comum/contrato_analise.py`.
Sai: `Decisoes` (dados puros): comandos de push (cada um com a mudanca de episodio que deve ser gravada junto),
alertas, registros de confirmacao, resumos de estado e descartes.

O que o nucleo NAO faz (e por que ele serve ao laco da analise, aos testes e as simulacoes):
  - nao le o relogio: o instante vem no argumento. O vigia (`vigiar`) tambem recebe o relogio de quem o chama,
    porque so ele depende do tempo que passa e nao do tempo dos dados;
  - nao tem thread, nao toca arquivo nem banco, nao envia nada. Quem grava e `analise/efeitos.py` (chamado pelo laco,
    `analise/laco.py`) e quem entrega o push e a borda, a partir de `comandos_push`;
  - nao guarda estado global: o estado e o `EstadoRegras` recebido. `PADRAO` aponta para as tabelas dos modulos
    de regra (usado por chamadas avulsas e testes); `novo_estado()` da um estado limpo e independente (o do laco).

Ordem das decisoes: para o mesmo instante, quem aplica deve aplicar `confirmacoes`, depois `alertas`/`estados`, depois
`comandos` (cada um grava episodio + comando juntos). E a ordem de `borda/efeitos.py`.

Validacao: a borda confere o envelope e o roteamento; o nucleo repete a conferencia estrutural do payload da fonte,
chamando o MESMO validador e os MESMOS schemas (`comum.contratos.validar`, contratos de schemas/), sem copia de regra.
A analise nao importa a borda: o que as duas partilham mora em `comum/`.
"""
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone

from comum import const as const_comum
from comum import contratos, mensagens
from comum.contrato_analise import (  # noqa: F401  (reexportados: o nucleo e quem os usa)
    Alerta, Comando, Confirmacao, Decisoes, Descarte, Leitura, MudancaEpisodio, Resposta, ResumoEstado, exigir_utc)
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
# Estado
# ============================================================================
@dataclass
class EstadoRegras:
    """O estado em memoria das tres regras. Cada `EstadoRegras` e um mundo a parte."""
    queda: regra_queda.EstadoQueda = field(default_factory=regra_queda.EstadoQueda)
    imobilidade: regra_imobilidade.EstadoImobilidade = field(default_factory=regra_imobilidade.EstadoImobilidade)
    luz: regra_luz.EstadoLuz = field(default_factory=regra_luz.EstadoLuz)


def limpar_estado(estado: EstadoRegras) -> None:
    """Esvazia, NO LUGAR, todas as tabelas do estado (quem guarda referencia a elas, como os modulos de regra com o PADRAO,
    continua enxergando as mesmas). Usado na restauracao da analise."""
    for sub in (estado.queda, estado.imobilidade, estado.luz):
        for f in fields(sub):
            valor = getattr(sub, f.name)
            if isinstance(valor, dict):
                valor.clear()


def novo_estado() -> EstadoRegras:
    """Estado limpo e independente (o do laco da analise, simulacoes e testes)."""
    return EstadoRegras()


# O estado PADRAO: as tabelas dos proprios modulos de regra (quem chama os modulos direto, como as chamadas avulsas e os
# testes, enxerga as mesmas tabelas). O laco da analise usa um estado proprio (`novo_estado()`).
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
    """Resumo do estado de imobilidade do dispositivo. Nunca levanta: um resumo perdido nao vale uma leitura."""
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
    ESTOU_BEM de um episodio aberto, encerra como CONFIRMADO. A resposta de outro usuario nao encerra nada."""
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
    exigir_utc(instante)
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