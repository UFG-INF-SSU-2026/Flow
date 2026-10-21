"""
Um ciclo da analise: ler o lote, produzir as saidas e avancar o cursor NUMA transacao so (item 3.6 do roadmap).

Sem Flask; usa so a interface RepoAnalise (banco real ou falso). E o ponto de integracao do split.

Garantias:
  - excecao em `processar` desfaz as saidas E o cursor; o proximo ciclo reprocessa o mesmo lote;
  - reprocessamento e at-least-once: `processar` deve ser idempotente por episodio_id / message_id;
  - o batimento do cursor (`atualizado_em`) e renovado mesmo sem leituras novas;
  - a leitura respeita a regra de lacunas de id (_lacunas.py).
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from . import config
from .repo_analise import RepoAnalise


def _somar_lacunas(metricas: dict | None, lacunas: int) -> dict | None:
    if not lacunas:
        return None                  # None = mantem as metricas como estao
    metricas = dict(metricas or {})
    metricas["lacunas_ignoradas"] = int(metricas.get("lacunas_ignoradas", 0)) + lacunas
    return metricas


def processar_lote(repo: RepoAnalise, processar: Callable[[RepoAnalise, list[dict]], None], *,
                   espera_lacuna_s: float = config.ESPERA_LACUNA_S, instante_minimo: datetime | None = None,
                   limite: int = config.LIMITE_LOTE) -> int:
    """Le, processa e avanca o cursor numa transacao. Devolve quantas leituras processou.

    `processar(tx, linhas)` recebe SO a transacao `tx` e grava alertas, comandos, episodios e estado por ela;
    escrever por outro caminho quebra a atomicidade.
    """
    cursor = repo.ler_cursor()
    linhas, lacunas = repo.ler_leituras_seguras(cursor["ultimo_leitura_id"], instante_minimo, limite, espera_lacuna_s)
    if not linhas:
        repo.gravar_cursor()         # so renova o batimento (distingue "analise parada" de "sem dado")
        return 0
    with repo.transacao() as tx:
        processar(tx, linhas)
        tx.gravar_cursor(ultimo_leitura_id=linhas[-1]["id"], ultimo_leitura_instante=linhas[-1]["instante_ingestao"],
                         metricas=_somar_lacunas(cursor.get("metricas"), lacunas))
    return len(linhas)


# ---------------------------------------------------------------------------
# Ciclo de TRES fluxos (S3a do split): leituras, respostas_dispositivo e entregas_push numa transacao so.
# ---------------------------------------------------------------------------
@dataclass
class Lote:
    """O que um ciclo leu, ja respeitando a regra de lacunas. Cada lista esta em ordem crescente de id."""
    leituras: list[dict] = field(default_factory=list)
    respostas: list[dict] = field(default_factory=list)
    entregas: list[dict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.leituras) + len(self.respostas) + len(self.entregas)

    def __bool__(self) -> bool:
        return self.total > 0


@dataclass(frozen=True)
class ResultadoCiclo:
    leituras: int
    respostas: int
    entregas: int
    lacunas: int
    cheio: bool                  # algum fluxo bateu no limite do lote: ha mais para ler, nao espere para o proximo ciclo

    @property
    def total(self) -> int:
        return self.leituras + self.respostas + self.entregas


def somar_metricas(base: dict | None, delta: dict | None) -> dict | None:
    """Soma contadores inteiros de `delta` em uma copia de `base`. None = nada a gravar (mantem as metricas)."""
    delta = {k: int(v) for k, v in (delta or {}).items() if v}
    if not delta:
        return None
    soma = dict(base or {})
    for chave, valor in delta.items():
        soma[chave] = int(soma.get(chave, 0)) + valor
    return soma


def processar_ciclo(repo: RepoAnalise, processar: Callable[[RepoAnalise, Lote], dict | None], *,
                    espera_lacuna_s: float = config.ESPERA_LACUNA_S, instante_minimo: datetime | None = None,
                    limite: int = config.LIMITE_LOTE, versao_codigo: str | None = None) -> ResultadoCiclo:
    """Le os tres fluxos, processa e avanca os tres cursores (e as metricas) NUMA transacao so.

    `processar(tx, lote)` recebe SO a transacao `tx` e devolve, se quiser, contadores para somar em
    `cursor_analise.metricas` (ex.: {"leituras_malformadas": 1}). As lacunas ignoradas dos tres fluxos entram na
    chave `lacunas_ignoradas`. Excecao em `processar` desfaz tudo (cursores incluidos): o proximo ciclo le o mesmo lote.
    Sem nada novo, so renova o batimento (o que distingue \"analise parada\" de \"sem dado\").
    """
    cursor = repo.ler_cursor()
    leituras, lac_l = repo.ler_leituras_seguras(cursor["ultimo_leitura_id"], instante_minimo, limite, espera_lacuna_s)
    respostas, lac_r = repo.ler_respostas_seguras(cursor["ultimo_resposta_id"], instante_minimo, limite, espera_lacuna_s)
    entregas, lac_e = repo.ler_entregas_seguras(cursor["ultimo_entrega_id"], instante_minimo, limite, espera_lacuna_s)
    lacunas = lac_l + lac_r + lac_e
    lote = Lote(leituras, respostas, entregas)
    cheio = any(len(l) >= limite for l in (leituras, respostas, entregas))
    resultado = ResultadoCiclo(len(leituras), len(respostas), len(entregas), lacunas, cheio)
    if not lote:
        repo.gravar_cursor(versao_codigo=versao_codigo)
        return resultado
    with repo.transacao() as tx:
        delta = dict(processar(tx, lote) or {})
        delta["lacunas_ignoradas"] = delta.get("lacunas_ignoradas", 0) + lacunas
        tx.gravar_cursor(
            ultimo_leitura_id=leituras[-1]["id"] if leituras else None,
            ultimo_leitura_instante=leituras[-1]["instante_ingestao"] if leituras else None,
            ultimo_resposta_id=respostas[-1]["id"] if respostas else None,
            ultimo_entrega_id=entregas[-1]["id"] if entregas else None,
            versao_codigo=versao_codigo,
            metricas=somar_metricas(cursor.get("metricas"), delta))
    return resultado
