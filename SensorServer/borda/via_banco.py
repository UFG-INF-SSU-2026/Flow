"""
Pipeline da BORDA ISOLADA (roadmap do split, S5): a borda nao decide e nao conhece a analise; o banco e o unico elo.

Mesma interface de `borda/caminho_direto.py` (e o que `borda/server.py` escolhe quando `const_borda.ISOLADA`), mas sem
nenhum import de `analise`: o processo da borda carrega so o seu pacote.

    analisar_leitura / analisar_resposta   nada a fazer: a leitura ja foi para a fila de descarga e a resposta ja foi
                                           gravada em `respostas_dispositivo` pela rota; quem decide e o OUTRO processo
    estado_do_dispositivo(id)              le `estado_dispositivo` (DS3), onde a analise o grava
    iniciar()                              sobe a thread que le `comandos_push` por cursor e entrega por SSE
    parar()                                desligamento ordenado: o ciclo de entrega em curso termina
"""
import threading
from datetime import datetime

from borda import entrega_push
from flow_db import repositorios


def analisar_leitura(user_id: int, device_id: str, fonte: str, dados: dict, versao: int, instante: datetime) -> None:
    return None


def analisar_resposta(user_id: int, device_id: str | None, fonte: str, evento: dict, versao: int,
                      instante: datetime) -> None:
    return None


def estado_do_dispositivo(device_id: str) -> dict | None:
    """Resumo gravado pela analise em `estado_dispositivo` (a forma e a mesma do caminho direto), ou None."""
    repo = repositorios.borda()
    linha = None if repo is None else repo.ler_estado_por_dispositivo(device_id)
    return None if linha is None else linha["estado"]


_entrega: "entrega_push.EntregaPush | None" = None


def iniciar_entrega() -> threading.Thread:
    global _entrega
    _entrega = entrega_push.EntregaPush()
    return _entrega.iniciar()


def parar_entrega(timeout: float = 5.0) -> None:
    if _entrega is not None:
        _entrega.parar(timeout)


def iniciar() -> None:
    """Chamada uma unica vez por borda/server.iniciar_threads."""
    iniciar_entrega()


def parar(timeout: float = 5.0) -> None:
    parar_entrega(timeout)
