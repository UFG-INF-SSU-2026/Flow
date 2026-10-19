"""
Um ciclo da analise: ler o lote, produzir as saidas e avancar o cursor NUMA transacao so (item 3.6 do roadmap).

Sem Flask; usa so a interface RepoAnalise (banco real ou falso). E o ponto de integracao do split.

Garantias:
  - excecao em `processar` desfaz as saidas E o cursor; o proximo ciclo reprocessa o mesmo lote;
  - reprocessamento e at-least-once: `processar` deve ser idempotente por episodio_id / message_id;
  - o batimento do cursor (`atualizado_em`) e renovado mesmo sem leituras novas;
  - a leitura respeita a regra de lacunas de id (_lacunas.py).
"""
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
