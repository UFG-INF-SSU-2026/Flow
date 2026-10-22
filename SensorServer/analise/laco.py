"""
Laco da ANALISE por cursor (roadmap do split, S3a).

Uma thread do mesmo processo le `leituras`, `respostas_dispositivo` e `entregas_push` NOVAS do banco por cursor (papel
flow_analise, regra de lacunas de id), transforma cada linha em entrada do nucleo e chama o nucleo das regras com o
INSTANTE DA INGESTAO gravado pela borda (principio 3). O batimento em `cursor_analise.atualizado_em` e renovado a cada
ciclo, mesmo sem dado novo.

O QUE O LACO FAZ NA S3a (e o que nao faz)
  - So REGISTRA as decisoes em log (e conta nas metricas do cursor). Nao grava alertas, confirmacoes, comandos,
    episodios nem estado: quem age continua sendo o caminho direto (borda/caminho_direto.py). Por isso o laco usa um
    `EstadoRegras` PROPRIO (`nucleo.novo_estado()`), nunca o `nucleo.PADRAO` do caminho direto: os dois mundos nao se
    enxergam, e a S3b compara o que cada um decidiu.
  - As entregas (`entregas_push`) nao alimentam o nucleo (ele nao tem entrada para elas); o laco as le, avanca o cursor
    e conta por resultado. Na S4 e a partir delas que a analise decide reenvio ou escalonamento (roadmap, secao 10).
  - Leitura de ESTADO por reprocessamento (janela anterior ao cursor) e do S6; aqui o estado das regras comeca vazio
    a cada subida do processo.

S4 (comum.const.ACOES_PELO_BANCO, `agir=True`): o laco passa a AGIR. Cada decisao sobre linha recente e gravada por
`efeitos.aplicar_na_transacao` na MESMA transacao do cursor (DS5); o vigia grava os encerramentos numa transacao propria.
Falha do banco desfaz o ciclo e o proximo reaproveita as decisoes ja tomadas (`_decisoes_por_linha`): o estado das regras
nao e desfeito e uma linha nunca e decidida duas vezes. Quem entrega o push e a borda (borda/entrega_push.py).

GARANTIAS
  - Leitura ou resposta MALFORMADA (nao vira entrada do nucleo) e falha numa regra sao tratadas POR LINHA: registra, conta
    em `cursor_analise.metricas` e segue; o cursor avanca. Uma linha ruim nunca trava nem derruba o laco.
  - Os tres cursores, as metricas e o batimento avancam na MESMA transacao (flow_db/ciclo_analise.processar_ciclo).
    Falha do banco no meio de um ciclo nao avanca nada; o laco registra (no maximo uma linha por
    `const_analise.INTERVALO_LOG_FALHA_S`) e tenta de novo no ciclo seguinte.
  - Linha mais velha que `const_analise.IDADE_MAX_ACAO_S` so reconstroi estado: a decisao sai marcada como
    "reconstrucao" (a S4 nao agira sobre ela).
  - O vigia (expiracao de episodios) e um PASSO SEPARADO que usa o relogio real, atrasado de ATRASO_VIGIA_S.
    Sem ele os episodios do laco nunca encerrariam e o estado divergiria do caminho direto.

ORDEM. Dentro de um ciclo, leituras e respostas sao processadas juntas, em ordem de instante de ingestao (estavel: em
empate, leituras antes de respostas). Os fluxos tem cursores independentes; um lote cortado pelo limite pode deixar
uma resposta para o ciclo seguinte, o que e inofensivo porque o ciclo seguinte comeca sem espera quando o lote enche.

Sem Flask e sem `borda` (conferido por tests/unit/test_fronteiras.py). A analise nao tem porta de rede.
"""
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

from analise import const as const_analise
from analise import nucleo
from comum import const as const_comum
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
# Registro das decisoes (S3a: so log)
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
      estado       EstadoRegras do laco (padrao: um novo, independente do estado do caminho direto)
      ao_decidir   funcao(decisoes, origem, reconstrucao) chamada para cada decisao nao vazia (padrao: log)
      ao_observar  S3b: funcao(decisoes, origem, reconstrucao) chamada para TODA `Decisoes` com algum conteudo, inclusive
                   so resumo de estado ou so descarte (que `ao_decidir` nao recebe). Alimenta a comparacao em modo
                   sombra (analise/sombra.py). Padrao None: nada muda.
      agir         S4: True = a analise AGE: grava comandos, alertas, confirmacoes, episodios e estado na MESMA transacao do
                   cursor (efeitos.aplicar_na_transacao) e o vigia grava os encerramentos. Decisao sobre linha mais velha
                   que IDADE_MAX_ACAO_S nao age (principio 4). Padrao False: so registra (S3a/S3b).
      traduzir_resposta  S3b: funcao(Resposta) -> Resposta aplicada a cada resposta ANTES do nucleo. So a sombra a usa
                   (troca o messageId do caminho direto pelo do pedido equivalente do laco). Padrao None.
    """

    def __init__(self, obter_repo: Callable[[], RepoAnalise | None] = repositorios.analise,
                 relogio: Callable[[], datetime] = _agora_real, estado: nucleo.EstadoRegras | None = None,
                 ao_decidir: Callable[[nucleo.Decisoes, str, bool], None] = logar_decisoes,
                 ao_observar: Callable[[nucleo.Decisoes, str, bool], None] | None = None,
                 traduzir_resposta: Callable[[nucleo.Resposta], nucleo.Resposta] | None = None,
                 agir: bool = False):
        self._obter_repo = obter_repo
        self._relogio = relogio
        self.estado = estado if estado is not None else nucleo.novo_estado()
        self._ao_decidir = ao_decidir
        self._ao_observar = ao_observar
        self._traduzir_resposta = traduzir_resposta
        self._agir = agir
        # --- so com agir (S4) ---
        self._estado_gravado: dict = {}                   # (user, device) -> assinatura do estado JA no banco (pos-commit)
        self._assinaturas_novas: dict = {}                # as da tentativa em curso (so entram em _estado_gravado no commit)
        self._depois_do_commit: list = []                 # arquivos/log da tentativa em curso, para rodar depois do commit
        self._decisoes_por_linha: dict = {}               # (fluxo, id) -> Decisoes ja decididas num ciclo que nao fez commit
        self._vigia_pendente = nucleo.Decisoes()          # encerramentos do vigia que ainda nao chegaram ao banco
        self._episodios_sem_acao: set = set()             # abertos por linha antiga (reconstrucao): o vigia nao age sobre eles
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
        versao = const_analise.VERSAO_CODIGO_ACOES if self._agir else const_analise.VERSAO_CODIGO
        resultado = ciclo_analise.processar_ciclo(repo, self._processar_lote, versao_codigo=versao)
        if self._agir:
            self._apos_commit()
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
        """Roda dentro da transacao do ciclo. Sem `agir` (S3a/S3b) NAO escreve por `_tx`: so decide e registra. Com `agir`
        (S4) grava as decisoes recentes por `_tx`."""
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
            if not eh_leitura and self._traduzir_resposta is not None:
                try:
                    item = self._traduzir_resposta(item)
                except Exception as e:  # noqa: BLE001  (sem traducao a resposta segue como esta)
                    print(f"  [laco] falha ao traduzir a resposta (ignorada): {type(e).__name__}")
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
            _contar(decisoes, metricas, antiga)
            if self._agir and not antiga:
                self._depois_do_commit += efeitos.aplicar_na_transacao(
                    _tx, decisoes, self._estado_gravado, self._assinaturas_novas)
            elif self._agir:
                self._episodios_sem_acao.update(c.episodio.episodio_id for c in decisoes.comandos)
            self._decidiu(decisoes, "leitura" if eh_leitura else "resposta", antiga)

        for e in lote.entregas:
            metricas[M_ENTREGAS] = metricas.get(M_ENTREGAS, 0) + 1
            resultado = e.get("resultado")
            chave = PREFIXO_ENTREGA + (resultado if isinstance(resultado, str) else "desconhecido")
            metricas[chave] = metricas.get(chave, 0) + 1
            if resultado != "enviado":
                print(f"  [laco] entrega sem sucesso resultado={resultado} canal={e.get('canal')} user={e.get('user_id')}")
        return metricas

    @staticmethod
    def _malformada(nome: str, linha: dict, erro: Exception, metricas: dict, chave: str) -> None:
        metricas[chave] = metricas.get(chave, 0) + 1
        print(f"  [laco] {nome} malformada id={linha.get('id')} ignorada (contada, o cursor avanca): {erro}")

    def _decidiu(self, decisoes: nucleo.Decisoes, origem: str, reconstrucao: bool) -> None:
        if self._ao_observar is not None and decisoes:     # `Decisoes.__bool__` inclui estados e descartes
            try:
                self._ao_observar(decisoes, origem, reconstrucao)
            except Exception as e:  # noqa: BLE001  (observar nunca derruba o laco)
                print(f"  [laco] falha ao observar a decisao (ignorada): {type(e).__name__}")
        if not (decisoes.comandos or decisoes.alertas or decisoes.confirmacoes):
            return
        try:
            self._ao_decidir(decisoes, origem, reconstrucao)
        except Exception as e:  # noqa: BLE001  (registrar nunca derruba o laco)
            print(f"  [laco] falha ao registrar a decisao (ignorada): {type(e).__name__}")

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
        """S4: grava os encerramentos EXPIRADO numa transacao propria. Se falhar, o estado das regras ja os registrou
        como encerrados: guarda-os e tenta de novo no proximo passo (nao se perdem). Nunca levanta."""
        if self._episodios_sem_acao:       # principio 4: episodio reconstruido de dado velho nao vira push nem registro
            decisoes.comandos[:] = [c for c in decisoes.comandos if c.episodio.episodio_id not in self._episodios_sem_acao]
            decisoes.confirmacoes[:] = [c for c in decisoes.confirmacoes
                                        if c.dados.get("episodeId") not in self._episodios_sem_acao]
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

