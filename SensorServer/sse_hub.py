"""
Mecanismo comum de push por SSE (Marco 3). O DISPOSITIVO abre a conexao; o servidor empurra por ela.
Registro por (user_id, canal) -> conexoes; fila por conexao; keepalive; replay do nao expirado.
"""
import queue
import threading
import time
from collections import deque

from flask import Response

import const   # KEEPALIVE_S, AVISO_REAUTENTICAR_S, TAMANHO_ANEL (lidos na hora do uso)

_FIM = object()


def ler_last_event_id(valor: str | None) -> int:
    """Cabecalho Last-Event-ID -> int. Ausente, vazio, nao numerico ou negativo vira 0 (DC-13), nunca excecao."""
    try:
        return max(int(valor or 0), 0)
    except ValueError:
        return 0


class Conexao:
    def __init__(self, device_id: str):
        self.device_id = device_id
        self.fila: queue.Queue = queue.Queue()

    def fechar(self):
        self.fila.put(_FIM)


class Hub:
    def __init__(self):
        self._lock = threading.Lock()
        self._conns: dict[tuple, dict[str, Conexao]] = {}
        self._anel: dict[tuple, deque] = {}
        self._contador: dict[tuple, int] = {}

    def ha_conexao(self, user_id: int, canal: str) -> bool:
        with self._lock:
            return bool(self._conns.get((user_id, canal)))

    def publicar(self, user_id: int, canal: str, mensagem: dict) -> int:
        """Guarda no anel (para replay) e entrega as conexoes ativas. Retorna quantas receberam."""
        chave = (user_id, canal)
        with self._lock:
            self._contador[chave] = self._contador.get(chave, 0) + 1
            eid = self._contador[chave]
            self._anel.setdefault(chave, deque(maxlen=const.TAMANHO_ANEL)).append((eid, mensagem))
            conns = list(self._conns.get(chave, {}).values())
        for c in conns:
            c.fila.put((eid, mensagem))
        return len(conns)

    def _registrar(self, chave, device_id, last_event_id, serializar):
        conn = Conexao(device_id)
        with self._lock:
            antiga = self._conns.setdefault(chave, {}).get(device_id)
            if antiga:
                antiga.fechar()            # nova conexao do mesmo device substitui a anterior
            self._conns[chave][device_id] = conn
            replay = [(eid, m) for eid, m in self._anel.get(chave, []) if eid > last_event_id]
        for eid, m in replay:              # so entra na fila o que ainda nao expirou (serializar devolve None se expirou)
            conn.fila.put((eid, m))
        return conn

    def _remover(self, chave, conn):
        with self._lock:
            if self._conns.get(chave, {}).get(conn.device_id) is conn:
                del self._conns[chave][conn.device_id]

    def resposta_sse(self, user_id: int, canal: str, device_id: str, exp_token: float,
                     last_event_id: int, serializar) -> Response:
        """serializar(mensagem) -> str (data cifrada) ou None se a mensagem expirou."""
        chave = (user_id, canal)
        conn = self._registrar(chave, device_id, last_event_id, serializar)

        def gerar():
            try:
                yield ": conectado\n\n"
                while True:
                    restante = exp_token - time.time()
                    if restante <= const.AVISO_REAUTENTICAR_S:
                        yield "event: reautenticar\ndata: {}\n\n"
                        return
                    try:
                        item = conn.fila.get(timeout=min(const.KEEPALIVE_S, max(restante - const.AVISO_REAUTENTICAR_S, 0.1)))
                    except queue.Empty:
                        yield ": keepalive\n\n"
                        continue
                    if item is _FIM:
                        return
                    eid, msg = item
                    data = serializar(msg)
                    if data is None:
                        continue
                    yield f"id: {eid}\nevent: push\ndata: {data}\n\n"
            finally:
                self._remover(chave, conn)

        return Response(gerar(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


HUB = Hub()
