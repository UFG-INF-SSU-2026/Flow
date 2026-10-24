"""
Mecanismo comum de push por SSE (Marco 3). O DISPOSITIVO abre a conexao; o servidor empurra por ela.
Registro por (user_id, canal) -> conexoes; fila por conexao; keepalive; replay do nao expirado.
"""
import queue
import threading
import time
from collections import deque

from flask import Response

from borda import const as const_borda   # KEEPALIVE_S, AVISO_REAUTENTICAR_S, TAMANHO_ANEL, TAMANHO_FILA_CONEXAO (lidos na hora do uso)

_FIM = object()


def ler_last_event_id(valor: str | None) -> int:
    """Cabecalho Last-Event-ID -> int. Ausente, vazio, nao numerico ou negativo vira 0, nunca excecao."""
    try:
        return max(int(valor or 0), 0)
    except ValueError:
        return 0


class Conexao:
    def __init__(self, device_id: str):
        self.device_id = device_id
        self.fila: queue.Queue = queue.Queue(maxsize=const_borda.TAMANHO_FILA_CONEXAO)

    def fechar(self):
        """Pede o encerramento da conexao. Toda escrita na fila acontece com o lock do Hub tomado, entao nao ha
        outro produtor concorrente. Fila cheia: descarta o que esta pendente (o cliente recupera pelo
        Last-Event-ID ao reconectar) para garantir que o marcador de fim entre."""
        try:
            self.fila.put_nowait(_FIM)
        except queue.Full:
            try:
                while True:
                    self.fila.get_nowait()
            except queue.Empty:
                pass
            self.fila.put_nowait(_FIM)


class Hub:
    def __init__(self):
        self._lock = threading.Lock()
        self._conns: dict[tuple, dict[str, Conexao]] = {}
        self._anel: dict[tuple, deque] = {}
        self._contador: dict[tuple, int] = {}
        # O contador de ids parte do instante de criacao do hub (segundos), para ser sempre maior que o de uma
        # execucao anterior: o Last-Event-ID guardado pelo cliente nunca fica a frente do contador depois de um
        # reinicio. Segundos, e nao milissegundos, porque o app guarda o id como Int de 32 bits.
        self._base_ids = int(time.time())

    def ha_conexao(self, user_id: int, canal: str) -> bool:
        with self._lock:
            return bool(self._conns.get((user_id, canal)))

    def publicar(self, user_id: int, canal: str, mensagem: dict) -> int:
        """Guarda no anel (para replay) e entrega as conexoes ativas. Retorna quantas receberam.
        Conexao com a fila cheia (cliente que nao consome) e fechada e retirada do registro: ao reconectar com
        Last-Event-ID ela recebe do anel o que ainda nao expirou. Nao conta como entregue."""
        chave = (user_id, canal)
        entregues = 0
        with self._lock:
            self._contador[chave] = self._contador.get(chave, self._base_ids) + 1
            eid = self._contador[chave]
            self._anel.setdefault(chave, deque(maxlen=const_borda.TAMANHO_ANEL)).append((eid, mensagem))
            for device_id, c in list(self._conns.get(chave, {}).items()):
                try:
                    c.fila.put_nowait((eid, mensagem))
                    entregues += 1
                except queue.Full:
                    del self._conns[chave][device_id]
                    c.fechar()
                    print(f"  [sse] fila cheia: conexao fechada user={user_id} canal={canal} device={device_id}")
        return entregues

    def fechar_todas(self) -> int:
        """Desligamento ordenado: encerra todos os streams. O cliente reconecta (Last-Event-ID) quando a borda
        voltar. Devolve quantas conexoes foram fechadas."""
        with self._lock:
            conns = [c for por_device in self._conns.values() for c in por_device.values()]
            for c in conns:
                c.fechar()
        return len(conns)

    def _registrar(self, chave, device_id, last_event_id, serializar):
        conn = Conexao(device_id)
        with self._lock:
            antiga = self._conns.setdefault(chave, {}).get(device_id)
            if antiga:
                antiga.fechar()            # nova conexao do mesmo device substitui a anterior
            # O replay entra na fila ANTES de a conexao ficar visivel para publicar(), dentro do mesmo lock:
            # uma publicacao concorrente nunca passa a frente e os ids saem em ordem crescente.
            # Cabe na fila (cheia = descartaria): se o anel tem mais pendentes que a fila, vale o mais recente.
            replay = [(eid, m) for eid, m in self._anel.get(chave, []) if eid > last_event_id]
            for item in replay[-const_borda.TAMANHO_FILA_CONEXAO:]:
                conn.fila.put_nowait(item)   # a serializacao descarta o que ja expirou, ao enviar
            self._conns[chave][device_id] = conn
        return conn

    def _remover(self, chave, conn):
        with self._lock:
            if self._conns.get(chave, {}).get(conn.device_id) is conn:
                del self._conns[chave][conn.device_id]

    def resposta_sse(self, user_id: int, canal: str, device_id: str, exp_token: float,
                     last_event_id: int, serializar) -> Response:
        """serializar(mensagem) -> str (data cifrada) ou None se a mensagem expirou."""
        chave = (user_id, canal)

        def gerar():
            conn = None
            try:
                # Registrar aqui dentro: se o cliente cair antes da primeira iteracao o gerador nunca roda
                # (e o finally tambem nao), entao nada pode ter sido registrado antes deste ponto.
                conn = self._registrar(chave, device_id, last_event_id, serializar)
                yield ": conectado\n\n"
                while True:
                    restante = exp_token - time.time()
                    if restante <= const_borda.AVISO_REAUTENTICAR_S:
                        yield "event: reautenticar\ndata: {}\n\n"
                        return
                    try:
                        item = conn.fila.get(timeout=min(const_borda.KEEPALIVE_S, max(restante - const_borda.AVISO_REAUTENTICAR_S, 0.1)))
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
                if conn is not None:
                    self._remover(chave, conn)

        return Response(gerar(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})   # sem Connection: e hop-by-hop (PEP 3333)


HUB = Hub()
