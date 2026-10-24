"""
Pipeline da BORDA: a borda nao decide e nao conhece a analise; o banco e o unico elo.

E o que `borda/server.py` usa para tudo que depende da analise. A leitura ja foi para a fila de descarga e a resposta do
dispositivo ja foi gravada em `respostas_dispositivo` pela propria rota: quem decide e o OUTRO processo
(`python -m analise`), que grava `comandos_push`, `alertas`, `confirmacoes`, `episodios` e `estado_dispositivo`.

    estado_do_dispositivo(id)   le `estado_dispositivo`, onde a analise grava o resumo
    iniciar()                   sobe a thread que le `comandos_push` por cursor e entrega por SSE
    parar()                     desligamento ordenado: o ciclo de entrega em curso termina

Nenhum import de `analise` (conferido por tests/unit/test_fronteiras.py): o processo da borda carrega so o seu pacote.
"""
import threading

from borda import entrega_push
from flow_db import repositorios


def estado_do_dispositivo(device_id: str) -> dict | None:
    """Resumo gravado pela analise em `estado_dispositivo` (a mesma forma da regra de imobilidade), ou None."""
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
