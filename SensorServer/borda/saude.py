"""
Saude do pipeline vista pela BORDA (roadmap do split, S6): o `/health` e os alarmes em log.

Com dois processos a borda segue aceitando dados enquanto ninguem decide queda se a analise parar. Para essa falha nao ser
silenciosa (roadmap, secao 8: "o ponto de maior risco do corte"), a borda le a VISAO `saude_pipeline` do banco (so o papel
flow_borda a le; `RepoBorda.ler_saude`) e a traduz em:

  atraso_leituras                   id da ultima leitura gravada menos o do cursor da analise
  idade_batimento_analise_s         ha quanto tempo a analise renovou o batimento (ela renova a cada ciclo, mesmo sem dado)
  idade_ultimo_comando_entregue_s   ha quanto tempo a borda entregou o ultimo comando de push (so informativo: sem queda
                                    nao ha comando, entao um valor alto e normal)

Estado do pipeline (`pipeline.estado` no corpo do /health):
  ok              batimento dentro do limiar e atraso dentro do limiar
  degradado       batimento velho ("analise_parada") e/ou atraso acima do limiar ("atraso_leituras")
  desconhecido    sem como ler a saude ("banco_indisponivel"): a borda nao sabe, e nao diz que esta tudo bem

CONTRATO. O `/health` continua respondendo 200 com `{"status": "ok"}` (a borda esta de pe), e SO ACRESCENTA as chaves
`pipeline` e `borda`: quem le apenas `status` nao percebe a mudanca. O corpo e publico (a rota ja era) e so tem contagens e
idades, nunca dado de usuario nem de conta.

A `MonitorSaude` tem duas faces: `corpo()` (o que o /health devolve, com cache curto: a rota e publica e nao pode virar uma
consulta ao banco por requisicao) e uma thread que consulta a cada `INTERVALO_MONITOR_SAUDE_S` e escreve os alarmes no log
(entrada em "degradado", mudanca de motivo, repeticao a cada `INTERVALO_REPETIR_ALARME_S` e volta ao normal). Falha do banco
nunca levanta: vira "desconhecido".

Este modulo nao importa a analise (conferido por tests/unit/test_fronteiras.py).
"""
import threading
import time
from typing import Callable

from borda import const as const_borda
from flow_db import repositorios

OK = "ok"
DEGRADADO = "degradado"
DESCONHECIDO = "desconhecido"

MOTIVO_ANALISE_PARADA = "analise_parada"
MOTIVO_SEM_BATIMENTO = "sem_batimento"
MOTIVO_ATRASO = "atraso_leituras"
MOTIVO_BANCO = "banco_indisponivel"


def _arredondar(valor):
    return None if valor is None else round(float(valor), 3)


def limiares() -> dict:
    return {"batimento_analise_s": const_borda.LIMIAR_BATIMENTO_ANALISE_S,
            "atraso_leituras": const_borda.LIMIAR_ATRASO_LEITURAS}


def classificar(saude: dict | None) -> dict:
    """Linha de `saude_pipeline` (ou None, se nao deu para le-la) -> o bloco `pipeline` do /health. Funcao pura: so
    compara com os limiares de const_borda, na hora do uso."""
    base = {"estado": DESCONHECIDO, "motivos": [MOTIVO_BANCO], "atraso_leituras": None,
            "idade_batimento_analise_s": None, "idade_ultimo_comando_entregue_s": None, "limiares": limiares()}
    if saude is None:
        return base
    atraso = saude.get("atraso_ids")
    batimento = saude.get("idade_batimento_analise_s")
    motivos = []
    if batimento is None:
        motivos.append(MOTIVO_SEM_BATIMENTO)
    elif batimento > const_borda.LIMIAR_BATIMENTO_ANALISE_S:
        motivos.append(MOTIVO_ANALISE_PARADA)
    if atraso is not None and atraso > const_borda.LIMIAR_ATRASO_LEITURAS:
        motivos.append(MOTIVO_ATRASO)
    base.update(estado=DEGRADADO if motivos else OK, motivos=motivos,
                atraso_leituras=None if atraso is None else int(atraso),
                idade_batimento_analise_s=_arredondar(batimento),
                idade_ultimo_comando_entregue_s=_arredondar(saude.get("idade_ultimo_comando_entregue_s")))
    return base


class MonitorSaude:
    """Consulta a saude do pipeline, guarda o ultimo resultado e escreve os alarmes.

    `obter_repo`   devolve o RepoBorda (None = sem banco: "desconhecido")
    `estatisticas` funcao sem argumento -> dict com os contadores LOCAIS da borda (fila de leituras e descartes), o bloco
                   `borda` do /health. O aumento dos descartes entre duas consultas vira alarme
    `relogio`      segundos monotonicos (injetavel nos testes)
    """

    def __init__(self, obter_repo: Callable = repositorios.borda,
                 estatisticas: Callable[[], dict] = dict, relogio: Callable[[], float] = time.monotonic):
        self._obter_repo = obter_repo
        self._estatisticas = estatisticas
        self._relogio = relogio
        self._lock = threading.Lock()
        self._cache: tuple | None = None            # (repo, instante, bloco pipeline)
        self._motivos_alarmados: tuple | None = None
        self._degradado_desde: float | None = None
        self._ultimo_alarme = float("-inf")
        self._descartes_vistos = 0
        self._ultimo_erro: str | None = None         # tipo da ultima excecao ao ler a saude (so o nome, para o alarme)
        self._parar = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- consulta ----
    def _consultar(self) -> dict:
        repo = self._obter_repo()
        if repo is None:
            return classificar(None)
        try:
            bloco = classificar(repo.ler_saude())
            self._ultimo_erro = None
            return bloco
        except Exception as e:  # noqa: BLE001  (o /health nunca vira 500 por causa do banco)
            self._ultimo_erro = type(e).__name__     # o alarme (com limite de repeticao) o informa; aqui nao ha log por consulta
            return classificar(None)

    def pipeline(self, forcar: bool = False) -> dict:
        """O bloco `pipeline`. Reaproveita a consulta de ate CACHE_SAUDE_S s (e so para o MESMO repositorio: um banco novo
        invalida o cache). `forcar` ignora o cache (a thread de monitoramento)."""
        repo = self._obter_repo()
        agora = self._relogio()
        with self._lock:
            if (not forcar and self._cache is not None and self._cache[0] is repo
                    and agora - self._cache[1] < const_borda.CACHE_SAUDE_S):
                return dict(self._cache[2])
            bloco = self._consultar()
            self._cache = (repo, self._relogio(), bloco)
            return dict(bloco)

    def corpo(self) -> dict:
        """O que o /health acrescenta a `{"status": "ok"}`."""
        try:
            local = dict(self._estatisticas())
        except Exception:  # noqa: BLE001
            local = {}
        return {"pipeline": self.pipeline(), "borda": local}

    # ---- alarmes ----
    def verificar(self) -> dict:
        """Uma passada do monitoramento: consulta (sem cache) e escreve os alarmes. Devolve o bloco `pipeline`."""
        bloco = self.pipeline(forcar=True)
        agora = self._relogio()
        self._alarmar_pipeline(bloco, agora)
        self._alarmar_descartes()
        return bloco

    def _alarmar_pipeline(self, bloco: dict, agora: float) -> None:
        motivos = tuple(bloco["motivos"])
        degradado = bloco["estado"] in (DEGRADADO, DESCONHECIDO)
        if not degradado:
            if self._degradado_desde is not None:
                print(f"  [saude] pipeline normalizado (ficou {agora - self._degradado_desde:.0f}s degradado)")
            self._degradado_desde, self._motivos_alarmados, self._ultimo_alarme = None, None, float("-inf")
            return
        novo = self._degradado_desde is None
        if novo:
            self._degradado_desde = agora
        if novo or motivos != self._motivos_alarmados or agora - self._ultimo_alarme >= const_borda.INTERVALO_REPETIR_ALARME_S:
            self._motivos_alarmados, self._ultimo_alarme = motivos, agora
            if bloco["estado"] == DESCONHECIDO:
                causa = f" ({self._ultimo_erro})" if self._ultimo_erro else ""
                print(f"  [saude] ALARME pipeline desconhecido: nao foi possivel ler a saude no banco{causa}; "
                      f"a borda nao sabe se a analise esta decidindo")
            else:
                print(f"  [saude] ALARME pipeline degradado: motivos={list(motivos)} atraso={bloco['atraso_leituras']} "
                      f"leitura(s), batimento da analise ha {bloco['idade_batimento_analise_s']}s "
                      f"(limiares: {const_borda.LIMIAR_ATRASO_LEITURAS} leituras, {const_borda.LIMIAR_BATIMENTO_ANALISE_S}s)")

    def _alarmar_descartes(self) -> None:
        try:
            total = int(self._estatisticas().get("leituras_descartadas", 0))
        except Exception:  # noqa: BLE001
            return
        if total > self._descartes_vistos:
            print(f"  [saude] ALARME leituras descartadas na borda: +{total - self._descartes_vistos} "
                  f"(total {total}); o banco nao esta recebendo a descarga e a fila encheu")
        self._descartes_vistos = total

    # ---- thread ----
    def _repetir(self) -> None:
        while not self._parar.wait(const_borda.INTERVALO_MONITOR_SAUDE_S):
            try:
                self.verificar()
            except Exception as e:  # noqa: BLE001  (a thread nunca pode morrer em silencio)
                print(f"  [saude] falha no monitoramento (a thread segue): {type(e).__name__}: {str(e)[:200]}")

    def iniciar(self) -> threading.Thread:
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._parar.clear()
        self._thread = threading.Thread(target=self._repetir, name="flow-saude", daemon=True)
        self._thread.start()
        return self._thread

    def parar(self, timeout: float = 5.0) -> None:
        self._parar.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self._thread = None
