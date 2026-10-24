"""
Laco da ANALISE por cursor.

Le `leituras`, `respostas_dispositivo` e `entregas_push` NOVAS do banco por cursor (papel flow_analise, regra de lacunas
de id), transforma cada linha em entrada do nucleo e chama o nucleo das regras com o INSTANTE DA INGESTAO gravado pela
borda. O batimento em `cursor_analise.atualizado_em` e renovado a cada ciclo, mesmo sem dado novo. E o
que `python -m analise` roda (analise/servico.py), numa thread propria.

O QUE O LACO FAZ
  - AGE (`agir=True`, o do servico): cada decisao sobre linha recente e gravada por `efeitos.aplicar_na_transacao` na
    MESMA transacao do cursor; o vigia grava os encerramentos numa transacao propria. Falha do banco desfaz o ciclo
    e o proximo reaproveita as decisoes ja tomadas (`_decisoes_por_linha`): o estado das regras nao e desfeito e uma
    linha nunca e decidida duas vezes. Quem entrega o push e a borda (borda/entrega_push.py), a partir de `comandos_push`.
  - ENSAIA (`agir=False`, o padrao do construtor; usado em testes e ferramentas): decide e so registra em log (e conta
    nas metricas do cursor), sem gravar alertas, confirmacoes, comandos, episodios nem estado.
  - Usa um `EstadoRegras` PROPRIO (`nucleo.novo_estado()`), nunca o `nucleo.PADRAO` dos modulos de regra.
  - As entregas (`entregas_push`) nao alimentam o nucleo (ele nao tem entrada para elas); o laco as le, avanca o cursor
    e conta por resultado: e a partir delas que a analise pode decidir reenvio ou escalonamento.

GARANTIAS
  - Leitura ou resposta MALFORMADA (nao vira entrada do nucleo) e falha numa regra sao tratadas POR LINHA: registra, conta
    em `cursor_analise.metricas` e segue; o cursor avanca. Uma linha ruim nunca trava nem derruba o laco.
  - Os tres cursores, as metricas e o batimento avancam na MESMA transacao (flow_db/ciclo_analise.processar_ciclo).
    Falha do banco no meio de um ciclo nao avanca nada; o laco registra (no maximo uma linha por
    `const_analise.INTERVALO_LOG_FALHA_S`) e tenta de novo no ciclo seguinte.
  - Linha mais velha que `const_analise.IDADE_MAX_ACAO_S` so reconstroi estado: a decisao sai marcada como
    "reconstrucao" e nao e gravada.
  - O vigia (expiracao de episodios) e um PASSO SEPARADO que usa o relogio real, atrasado de ATRASO_VIGIA_S.

OBSERVABILIDADE E RESILIENCIA:
  - RECUPERACAO NA SUBIDA (`recuperar_estado`, chamada pela thread antes do primeiro ciclo quando o laco AGE): rele as
    leituras dos ultimos `JANELA_REPROCESSAMENTO_S` s ate o cursor para reconstruir as janelas das regras (maquina de
    impacto, fusao, imobilidade, luz, deduplicacao) SEM gerar acao, e devolve ao estado os episodios que o banco ja
    conhece (abertos, e os recentes, para cooldown e resposta tardia) com as `messageId` dos seus pedidos. Depois disso o
    laco segue do cursor: so age sobre linha recente. Se o banco falhar na recuperacao, nenhum ciclo roda
    (o cursor nem avancaria) e a thread tenta de novo.
  - LINHA ANTIGA, duas regras: (1) o episodio que uma linha antiga abre e ficcao (nao virou push nem
    registro) e SAI do estado na hora, para nao bloquear nem por em cooldown a queda real que vier logo depois;
    (2) a resposta antiga que encerra um episodio que o banco ja tem (o idoso respondeu enquanto a analise estava parada)
    NAO e descartada: e escrituracao de um episodio real, e sem ela ele ficaria aberto no banco para sempre.
  - ALARMES em log: atraso (a leitura mais antiga do ciclo passou de `LIMIAR_ATRASO_ANALISE_S`) e leituras/respostas
    descartadas por idade (so reconstroem estado). No maximo uma linha por `INTERVALO_LOG_FALHA_S`, com o contador
    acumulado: nada se perde entre uma linha e outra. O batimento velho e acusado do outro lado, pelo /health da borda.

ORDEM. Dentro de um ciclo, leituras e respostas sao processadas juntas, em ordem de instante de ingestao (estavel: em
empate, leituras antes de respostas). Os fluxos tem cursores independentes; um lote cortado pelo limite pode deixar
uma resposta para o ciclo seguinte, o que e inofensivo porque o ciclo seguinte comeca sem espera quando o lote enche.

Sem Flask e sem `borda` (conferido por tests/unit/test_fronteiras.py). A analise nao tem porta de rede.
"""
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from analise import const as const_analise
from analise import nucleo, regra_queda
from comum import const as const_comum
from comum import mensagens
from analise import efeitos
from flow_db import ciclo_analise, repositorios
from flow_db.repo_analise import RepoAnalise

# Chaves de `cursor_analise.metricas` escritas pelo laco (contadores que so crescem).
M_LEITURAS = "leituras_processadas"
M_RESPOSTAS = "respostas_processadas"
M_ENTREGAS = "entregas_lidas"
M_LEITURAS_MALFORMADAS = "leituras_malformadas"
M_RESPOSTAS_MALFORMADAS = "respostas_malformadas"
M_FALHAS = "itens_com_falha"
M_LEITURAS_ANTIGAS = "leituras_antigas"
M_RESPOSTAS_ANTIGAS = "respostas_antigas"
M_DECISOES_ANTIGAS = "decisoes_antigas"
M_COMANDOS = "decisoes_comandos"
M_ALERTAS = "decisoes_alertas"
M_CONFIRMACOES = "decisoes_confirmacoes"
PREFIXO_DESCARTE = "descartes_"          # + motivo do nucleo (estrutura_invalida, duplicada...)
PREFIXO_ENTREGA = "entregas_"            # + resultado (enviado, sem_conexao...)


class LinhaMalformada(ValueError):
    """A linha do banco nao pode virar entrada do nucleo. A mensagem NUNCA contem conteudo da linha."""


# ============================================================================
# Linha do banco -> entrada do nucleo
# ============================================================================
def _inteiro(valor) -> bool:
    return isinstance(valor, int) and not isinstance(valor, bool)


def _conferir_comum(linha: dict) -> None:
    if not _inteiro(linha.get("user_id")):
        raise LinhaMalformada("user_id ausente ou nao inteiro")
    if linha.get("fonte") not in const_comum.FONTES_VALIDAS:
        raise LinhaMalformada("fonte desconhecida")
    if not _inteiro(linha.get("schema_version")):
        raise LinhaMalformada("schema_version ausente ou nao inteiro")
    instante = linha.get("instante_ingestao")
    if not isinstance(instante, datetime) or instante.tzinfo is None:
        raise LinhaMalformada("instante_ingestao ausente ou sem fuso")


def leitura_de_linha(linha: dict) -> nucleo.Leitura:
    """Linha de `leituras` -> `nucleo.Leitura`. Levanta LinhaMalformada se faltar algo que o nucleo exige."""
    _conferir_comum(linha)
    if not isinstance(linha.get("device_id"), str) or not linha["device_id"]:
        raise LinhaMalformada("device_id ausente")
    if not isinstance(linha.get("payload"), dict):
        raise LinhaMalformada("payload nao e um objeto")
    return nucleo.Leitura(linha["instante_ingestao"], linha["user_id"], linha["device_id"], linha["fonte"],
                          linha["payload"], linha["schema_version"])


def resposta_de_linha(linha: dict) -> nucleo.Resposta:
    """Linha de `respostas_dispositivo` -> `nucleo.Resposta`. A identidade vem da linha (gravada a partir do token)."""
    _conferir_comum(linha)
    device_id = linha.get("device_id")
    if device_id is not None and not isinstance(device_id, str):
        raise LinhaMalformada("device_id nao e texto")
    if not isinstance(linha.get("payload"), dict):
        raise LinhaMalformada("payload nao e um objeto")
    return nucleo.Resposta(linha["instante_ingestao"], linha["user_id"], device_id, linha["fonte"],
                           linha["payload"], linha["schema_version"])


# ============================================================================
# Registro das decisoes (log)
# ============================================================================
def _iso(instante: datetime) -> str:
    return instante.astimezone(timezone.utc).isoformat()


def logar_decisoes(decisoes: nucleo.Decisoes, origem: str, reconstrucao: bool) -> None:
    """Destino padrao das decisoes: uma linha de log por comando, alerta e confirmacao. Nunca imprime o conteudo da
    leitura nem da mensagem (so tipos, ids e instantes). `origem` e "leitura", "resposta" ou "vigia"."""
    marca = " (reconstrucao, sem acao)" if reconstrucao else ""
    for c in decisoes.comandos:
        print(f"  [laco] decisao{marca} origem={origem} comando={c.mensagem['messageType']} user={c.user_id} "
              f"episodio={c.episodio.episodio_id} estado={c.episodio.estado} instante={_iso(c.criado_em)}")
    for a in decisoes.alertas:
        print(f"  [laco] decisao{marca} origem={origem} alerta={a.tipo} user={a.user_id} device={a.device_id} "
              f"instante={_iso(a.instante)}")
    for c in decisoes.confirmacoes:
        print(f"  [laco] decisao{marca} origem={origem} registro={c.tipo} user={c.user_id} instante={_iso(c.instante)}")


def _contar(decisoes: nucleo.Decisoes, metricas: dict, antiga: bool) -> None:
    metricas[M_COMANDOS] = metricas.get(M_COMANDOS, 0) + len(decisoes.comandos)
    metricas[M_ALERTAS] = metricas.get(M_ALERTAS, 0) + len(decisoes.alertas)
    metricas[M_CONFIRMACOES] = metricas.get(M_CONFIRMACOES, 0) + len(decisoes.confirmacoes)
    for d in decisoes.descartes:
        chave = PREFIXO_DESCARTE + d.motivo
        metricas[chave] = metricas.get(chave, 0) + 1
    if antiga:
        n = len(decisoes.comandos) + len(decisoes.alertas) + len(decisoes.confirmacoes)
        metricas[M_DECISOES_ANTIGAS] = metricas.get(M_DECISOES_ANTIGAS, 0) + n


@dataclass(frozen=True)
class ResultadoRecuperacao:
    """O que a recuperacao na subida fez."""
    leituras_relidas: int       # leituras reprocessadas (so para reconstruir estado, sem acao)
    leituras_ignoradas: int     # malformadas ou com falha na regra: ja contadas quando foram processadas de verdade
    episodios: int              # episodios devolvidos ao estado (abertos e recentes)
    abertos: int                # destes, os ainda abertos (o vigia vai encerra-los no prazo)


# ============================================================================
# O laco
# ============================================================================
def _agora_real() -> datetime:
    return datetime.now(timezone.utc)


class LacoAnalise:
    """O laco da analise. `executar_ciclo()` e um ciclo SINCRONO (sem thread, sem espera): e o que os testes chamam.
    `iniciar()` sobe a thread que o repete.

    Parametros (todos opcionais; o padrao e o servidor real):
      obter_repo   funcao que devolve o RepoAnalise (None = sem banco; o ciclo nao faz nada)
      relogio      relogio REAL (so o vigia e a idade das linhas o usam)
      estado       EstadoRegras do laco (padrao: um novo, independente do `nucleo.PADRAO`)
      ao_decidir   funcao(decisoes, origem, reconstrucao) chamada para cada decisao nao vazia (padrao: log)
      agir         True = a analise AGE: grava comandos, alertas, confirmacoes, episodios e estado na MESMA transacao do
                   cursor (efeitos.aplicar_na_transacao) e o vigia grava os encerramentos. Decisao sobre linha mais velha
                   que IDADE_MAX_ACAO_S nao age. Padrao False: so registra (ensaio).
      recuperar    a thread (`iniciar`) roda `recuperar_estado()` antes do primeiro ciclo. Padrao None = so quando o laco
                   AGE e const_analise.RECUPERAR_AO_INICIAR. `executar_ciclo()` nunca recupera sozinho (os testes chamam
                   `recuperar_estado()` quando querem).
    """

    def __init__(self, obter_repo: Callable[[], RepoAnalise | None] = repositorios.analise,
                 relogio: Callable[[], datetime] = _agora_real, estado: nucleo.EstadoRegras | None = None,
                 ao_decidir: Callable[[nucleo.Decisoes, str, bool], None] = logar_decisoes,
                 agir: bool = False, recuperar: bool | None = None):
        self._obter_repo = obter_repo
        self._relogio = relogio
        self.estado = estado if estado is not None else nucleo.novo_estado()
        self._ao_decidir = ao_decidir
        self._agir = agir
        self._recuperar = (agir and const_analise.RECUPERAR_AO_INICIAR) if recuperar is None else recuperar
        self.recuperado = False                           # True depois de recuperar_estado() concluir
        self._ultimo_lote: tuple | None = None            # (idade da leitura mais antiga, metricas) do ciclo em curso
        self._alarme: dict = {"leituras": 0, "respostas": 0, "decisoes": 0, "atraso_s": 0.0}
        self._ultimo_alarme = float("-inf")
        # --- so com agir ---
        self._estado_gravado: dict = {}                   # (user, device) -> assinatura do estado JA no banco (pos-commit)
        self._assinaturas_novas: dict = {}                # as da tentativa em curso (so entram em _estado_gravado no commit)
        self._depois_do_commit: list = []                 # arquivos/log da tentativa em curso, para rodar depois do commit
        self._decisoes_por_linha: dict = {}               # (fluxo, id) -> Decisoes ja decididas num ciclo que nao fez commit
        self._vigia_pendente = nucleo.Decisoes()          # encerramentos do vigia que ainda nao chegaram ao banco
        self._parar = threading.Event()
        self._thread: threading.Thread | None = None
        self._ultimo_log_falha = float("-inf")
        self.falhas_seguidas = 0

    # ---- um ciclo ----
    def executar_ciclo(self) -> ciclo_analise.ResultadoCiclo | None:
        """Le o que ha de novo, processa e avanca os cursores; depois, o passo do vigia. Devolve o resultado da leitura
        (None se nao ha banco). Excecao do banco SOBE (o chamador decide: a thread registra e tenta de novo)."""
        repo = self._obter_repo()
        if repo is None:
            return None
        self._assinaturas_novas, self._depois_do_commit = {}, []      # tentativa nova (a anterior, se falhou, foi desfeita)
        self._ultimo_lote = None
        versao = const_analise.VERSAO_CODIGO_ACOES if self._agir else const_analise.VERSAO_CODIGO
        resultado = ciclo_analise.processar_ciclo(repo, self._processar_lote, versao_codigo=versao)
        if self._agir:
            self._apos_commit()
        self._alarmar()
        self.passo_vigia()
        return resultado

    def _apos_commit(self) -> None:
        """O ciclo chegou ao commit: o que foi gravado vale, e o que ficou para depois (arquivos, log) roda agora."""
        self._estado_gravado.update(self._assinaturas_novas)
        self._decisoes_por_linha.clear()
        pendentes, self._depois_do_commit = self._depois_do_commit, []
        for fazer in pendentes:
            try:
                fazer()
            except Exception as e:  # noqa: BLE001  (arquivo de transicao nunca desfaz o que ja esta no banco)
                print(f"  [laco] falha em efeito pos-commit (ignorada): {type(e).__name__}: {str(e)[:150]}")

    def _processar_lote(self, _tx: RepoAnalise, lote: ciclo_analise.Lote) -> dict:
        """Roda dentro da transacao do ciclo. Sem `agir` (ensaio) NAO escreve por `_tx`: so decide e registra. Com `agir`
        grava as decisoes recentes por `_tx`."""
        metricas: dict = {}
        agora = self._relogio()
        limite = const_analise.IDADE_MAX_ACAO_S
        itens: list = []     # (chave da linha, item); leituras antes de respostas: em empate a ordem se mantem (sort estavel)

        for linha in lote.leituras:
            try:
                itens.append((("leitura", linha.get("id")), leitura_de_linha(linha)))
            except ValueError as e:     # inclui LinhaMalformada
                self._malformada("leitura", linha, e, metricas, M_LEITURAS_MALFORMADAS)
        for linha in lote.respostas:
            try:
                itens.append((("resposta", linha.get("id")), resposta_de_linha(linha)))
            except ValueError as e:     # inclui LinhaMalformada
                self._malformada("resposta", linha, e, metricas, M_RESPOSTAS_MALFORMADAS)

        for chave_linha, item in sorted(itens, key=lambda ci: ci[1].instante):
            eh_leitura = isinstance(item, nucleo.Leitura)
            antiga = (agora - item.instante).total_seconds() > limite
            try:
                decisoes = self._decisoes_por_linha.get(chave_linha) if self._agir else None
                if decisoes is None:        # (com agir, uma linha de um ciclo desfeito NAO e decidida de novo: o estado ja a viu)
                    decisoes = (nucleo.processar_leitura if eh_leitura else nucleo.processar_resposta)(self.estado, item)
                    if self._agir:
                        self._decisoes_por_linha[chave_linha] = decisoes
            except Exception as e:  # noqa: BLE001  (uma regra com bug nao pode travar o cursor)
                metricas[M_FALHAS] = metricas.get(M_FALHAS, 0) + 1
                print(f"  [laco] falha ao processar {'leitura' if eh_leitura else 'resposta'} user={item.user_id} "
                      f"(contada, o ciclo segue): {type(e).__name__}")
                continue
            chave = M_LEITURAS if eh_leitura else M_RESPOSTAS
            metricas[chave] = metricas.get(chave, 0) + 1
            if antiga:
                chave = M_LEITURAS_ANTIGAS if eh_leitura else M_RESPOSTAS_ANTIGAS
                metricas[chave] = metricas.get(chave, 0) + 1
            # Linha antiga so reconstroi estado, EXCETO a resposta que encerra um episodio que o banco ja tem (o idoso
            # disse "Estou bem" enquanto a analise estava parada): isso e escrituracao de um episodio real, e sem ela o
            # episodio ficaria aberto no banco para sempre. O encerramento sai com a validade do instante da resposta, e a
            # borda o marca como `expirado` em vez de entregar.
            sem_acao = antiga and not (self._agir and not eh_leitura and decisoes.comandos)
            _contar(decisoes, metricas, sem_acao)
            if self._agir and not sem_acao:
                self._depois_do_commit += efeitos.aplicar_na_transacao(
                    _tx, decisoes, self._estado_gravado, self._assinaturas_novas)
            elif self._agir:
                # O episodio que a linha antiga abriu e ficcao: nao virou push nem registro. Sai do estado AGORA, senao
                # bloquearia (aberto) ou poria em cooldown (encerrado) a queda real que vem logo depois.
                for c in decisoes.comandos:
                    regra_queda.descartar_episodio(c.user_id, c.episodio.episodio_id, self.estado.queda)
            self._decidiu(decisoes, "leitura" if eh_leitura else "resposta", sem_acao)

        for e in lote.entregas:
            metricas[M_ENTREGAS] = metricas.get(M_ENTREGAS, 0) + 1
            resultado = e.get("resultado")
            chave = PREFIXO_ENTREGA + (resultado if isinstance(resultado, str) else "desconhecido")
            metricas[chave] = metricas.get(chave, 0) + 1
            if resultado != "enviado":
                print(f"  [laco] entrega sem sucesso resultado={resultado} canal={e.get('canal')} user={e.get('user_id')}")
        idade = (agora - min(item.instante for _, item in itens)).total_seconds() if itens else None
        self._ultimo_lote = (idade, dict(metricas))      # o alarme so e emitido depois do commit (ciclo refeito nao duplica)
        return metricas

    @staticmethod
    def _malformada(nome: str, linha: dict, erro: Exception, metricas: dict, chave: str) -> None:
        metricas[chave] = metricas.get(chave, 0) + 1
        print(f"  [laco] {nome} malformada id={linha.get('id')} ignorada (contada, o cursor avanca): {erro}")

    def _decidiu(self, decisoes: nucleo.Decisoes, origem: str, reconstrucao: bool) -> None:
        if not (decisoes.comandos or decisoes.alertas or decisoes.confirmacoes):
            return
        try:
            self._ao_decidir(decisoes, origem, reconstrucao)
        except Exception as e:  # noqa: BLE001  (registrar nunca derruba o laco)
            print(f"  [laco] falha ao registrar a decisao (ignorada): {type(e).__name__}")

    # ---- recuperacao na subida ----
    def recuperar_estado(self) -> ResultadoRecuperacao | None:
        """Reconstroi o estado das regras depois de uma subida. Devolve None se nao ha banco.

        1. RELE (so leitura) as leituras dos ultimos `JANELA_REPROCESSAMENTO_S` s ate o cursor (as mais recentes, ate
           `LIMITE_REPROCESSAMENTO`) e as passa pelo nucleo num estado NOVO, em ordem de ingestao. As decisoes saem e sao
           DESCARTADAS: nada e gravado, nada e entregue. So interessa o que ficou na memoria das regras (maquina do
           impacto, fusao de fontes, imobilidade, luz, deduplicacao). A janela e ancorada no instante da ultima leitura
           processada (nao no relogio): reconstroi o estado como ele estava no cursor, qualquer que tenha sido a parada.
        2. RESTAURA os episodios pelo banco, nao pela releitura: a releitura de uma queda ja tratada abriria um episodio
           novo (outro id) e o vigia o encerraria como se fosse real. Por isso os episodios e o mapa de mensagens que a
           releitura deixou sao descartados e substituidos pelos de `episodios` e `comandos_push` (abertos e atualizados na
           ultima `RETENCAO_MENSAGENS_S`): o aberto continua aberto com as mesmas `messageId` (a resposta do idoso ainda o
           encerra), o encerrado ainda vale para o cooldown, e a resposta a um episodio velho e classificada como tardia.
        3. As respostas NAO sao relidas: so mexem em episodios e mensagens, que vem do banco (passo 2).

        Tudo e lido ANTES de o estado ser trocado: se o banco falhar no meio, `self.estado` fica como estava e a excecao
        SOBE (a thread tenta de novo; sem recuperar, nenhum ciclo age). Chame uma vez, antes do primeiro ciclo: o
        estado anterior e substituido. Idempotente (rele o mesmo que da outra vez)."""
        repo = self._obter_repo()
        if repo is None:
            return None
        agora = self._relogio()
        cursor = repo.ler_cursor()
        referencia = cursor.get("ultimo_leitura_instante") or agora
        linhas = repo.ler_leituras_recentes(cursor["ultimo_leitura_id"],
                                            referencia - timedelta(seconds=const_analise.JANELA_REPROCESSAMENTO_S),
                                            const_analise.LIMITE_REPROCESSAMENTO)
        episodios = repo.listar_episodios_recentes(agora - timedelta(seconds=const_analise.RETENCAO_MENSAGENS_S))
        comandos = repo.listar_comandos_de_episodios([e["episodio_id"] for e in episodios])

        novo = nucleo.novo_estado()
        ignoradas, itens = 0, []
        for linha in linhas:
            try:
                itens.append(leitura_de_linha(linha))
            except ValueError:      # inclui LinhaMalformada (ja contada quando a linha foi processada de verdade)
                ignoradas += 1
        for leitura in sorted(itens, key=lambda l: l.instante):
            try:
                nucleo.processar_leitura(novo, leitura)         # decisoes descartadas: reconstrucao pura
            except Exception:  # noqa: BLE001  (uma regra com bug nao pode impedir a subida)
                ignoradas += 1

        regra_queda.limpar_episodios(novo.queda)                # o que a releitura abriu e fantasma
        mensagens_do_episodio: dict[str, list[str]] = {}
        for c in comandos:
            if c.get("tipo") == mensagens.TIPO_SOLICITADA and c.get("message_id"):
                mensagens_do_episodio.setdefault(c["episodio_id"], []).append(c["message_id"])
        abertos = 0
        for e in episodios:                                     # ordem crescente de aberto_em
            encerrado = e["estado"] == "encerrado"
            fim = (e.get("encerrado_em") or e["atualizado_em"]) if encerrado else None
            regra_queda.restaurar_episodio(
                e["user_id"], e["episodio_id"], e["tipo"], e["aberto_em"].timestamp(),
                None if fim is None else fim.timestamp(), e.get("motivo_encerramento") if encerrado else None,
                mensagens_do_episodio.get(e["episodio_id"], ()), novo.queda)
            abertos += 0 if encerrado else 1

        self.estado = novo                                      # troca no fim: nada fica pela metade
        self.recuperado = True
        resultado = ResultadoRecuperacao(len(itens), ignoradas, len(episodios), abertos)
        print(f"  [laco] recuperacao: {resultado.leituras_relidas} leitura(s) relida(s) (janela de "
              f"{const_analise.JANELA_REPROCESSAMENTO_S}s ate o cursor), {resultado.episodios} episodio(s) restaurado(s), "
              f"{resultado.abertos} aberto(s)")
        return resultado

    # ---- alarmes ----
    def _alarmar(self) -> None:
        """Alarmes de atraso e de leituras/respostas descartadas por idade, depois do commit do ciclo. Acumula entre as
        linhas de log (no maximo uma por INTERVALO_LOG_FALHA_S): nada que aconteceu entre duas linhas se perde. Nunca levanta."""
        try:
            if self._ultimo_lote is None:
                return
            idade, metricas = self._ultimo_lote
            acum = self._alarme
            acum["leituras"] += metricas.get(M_LEITURAS_ANTIGAS, 0)
            acum["respostas"] += metricas.get(M_RESPOSTAS_ANTIGAS, 0)
            acum["decisoes"] += metricas.get(M_DECISOES_ANTIGAS, 0)
            if idade is not None:
                acum["atraso_s"] = max(acum["atraso_s"], idade)
            atrasada = acum["atraso_s"] > const_analise.LIMIAR_ATRASO_ANALISE_S
            antigas = acum["leituras"] or acum["respostas"]
            agora = self._relogio().timestamp()
            if not (atrasada or antigas) or agora - self._ultimo_alarme < const_analise.INTERVALO_LOG_FALHA_S:
                return
            self._ultimo_alarme = agora
            if atrasada:
                print(f"  [laco] ALARME atraso: a leitura mais antiga lida tinha {acum['atraso_s']:.1f}s "
                      f"(limiar {const_analise.LIMIAR_ATRASO_ANALISE_S}s); a analise esta atrasada")
            if antigas:
                print(f"  [laco] ALARME descartadas por idade: {acum['leituras']} leitura(s) e {acum['respostas']} "
                      f"resposta(s) com mais de {const_analise.IDADE_MAX_ACAO_S}s so reconstruiram estado, sem acao "
                      f"({acum['decisoes']} decisao(oes) nao executada(s))")
            self._alarme = {"leituras": 0, "respostas": 0, "decisoes": 0, "atraso_s": 0.0}
        except Exception as e:  # noqa: BLE001  (alarme nunca derruba o laco)
            print(f"  [laco] falha ao emitir alarme (ignorada): {type(e).__name__}")

    # ---- vigia ----
    def passo_vigia(self) -> nucleo.Decisoes:
        """Encerra como EXPIRADO os episodios vencidos NO ESTADO DO LACO. Unico passo com relogio real (atrasado de
        ATRASO_VIGIA_S, para o dado em transito ser lido antes). Nunca levanta."""
        try:
            instante = self._relogio().astimezone(timezone.utc)
            decisoes = nucleo.vigiar(self.estado, instante - timedelta(seconds=const_analise.ATRASO_VIGIA_S))
        except Exception as e:  # noqa: BLE001
            print(f"  [laco] falha no vigia (a thread segue): {type(e).__name__}: {str(e)[:200]}")
            return nucleo.Decisoes()
        if self._agir:
            self._gravar_vigia(decisoes)
        if decisoes.comandos or decisoes.alertas or decisoes.confirmacoes:
            self._decidiu(decisoes, "vigia", False)
        return decisoes

    def _gravar_vigia(self, decisoes: nucleo.Decisoes) -> None:
        """Grava os encerramentos EXPIRADO numa transacao propria. Se falhar, o estado das regras ja os registrou
        como encerrados: guarda-os e tenta de novo no proximo passo (nao se perdem). Nunca levanta."""
        self._vigia_pendente.somar(decisoes)
        if not self._vigia_pendente:
            return
        repo = self._obter_repo()
        if repo is None:
            return
        try:
            with repo.transacao() as tx:
                depois = efeitos.aplicar_na_transacao(tx, self._vigia_pendente, self._estado_gravado, {})
        except Exception as e:  # noqa: BLE001
            self._falha(e)
            return
        self._vigia_pendente = nucleo.Decisoes()
        for fazer in depois:
            try:
                fazer()
            except Exception as e:  # noqa: BLE001
                print(f"  [laco] falha em efeito pos-commit do vigia (ignorada): {type(e).__name__}: {str(e)[:150]}")

    # ---- thread ----
    def _falha(self, e: Exception) -> None:
        self.falhas_seguidas += 1
        agora = self._relogio().timestamp()
        if agora - self._ultimo_log_falha >= const_analise.INTERVALO_LOG_FALHA_S:
            self._ultimo_log_falha = agora
            print(f"  [laco] falha no ciclo (cursor nao avancou; tenta de novo): {type(e).__name__}: {str(e)[:200]} "
                  f"[falhas seguidas: {self.falhas_seguidas}]")

    def _repetir(self) -> None:
        while not self._parar.is_set():
            if self._recuperar and not self.recuperado:
                try:
                    self.recuperar_estado()
                except Exception as e:  # noqa: BLE001  (sem recuperar nenhum ciclo age: tenta de novo)
                    self._falha(e)
                if not self.recuperado:
                    self._parar.wait(const_analise.INTERVALO_LEITURA_S)
                continue
            cheio = False
            try:
                resultado = self.executar_ciclo()
                cheio = bool(resultado and resultado.cheio)
                self.falhas_seguidas = 0
            except Exception as e:  # noqa: BLE001  (a thread nunca pode morrer em silencio)
                self._falha(e)
            if not cheio:       # lote cheio = ha mais para ler: nao espera
                self._parar.wait(const_analise.INTERVALO_LEITURA_S)

    def iniciar(self) -> threading.Thread:
        """Sobe a thread do laco (uma vez). Devolve a thread."""
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._parar.clear()
        self._thread = threading.Thread(target=self._repetir, name="flow-laco-analise", daemon=True)
        self._thread.start()
        return self._thread

    def parar(self, timeout: float = 5.0) -> None:
        """Pede o fim e espera o ciclo em curso terminar (o lote em andamento chega ao commit do cursor)."""
        self._parar.set()
        if self._thread is not None:
            self._thread.join(timeout)

