"""Borda falsa em TLS (Werkzeug threaded, HTTP/1.1 com chunks, como a borda real), em thread, em loopback e porta 0.

Registra cada requisicao recebida e responde pelo roteiro de cada teste. O push (SSE) e controlado pelo teste: ele empurra
quadros (`emitir`) e ve quando a conexao foi fechada pelo outro lado (`sse_fechado`)."""
import queue
import ssl
import threading
import time

from werkzeug.serving import make_server

from apoio import HOST, Pki

FIM = object()


class BordaFalsa:
    def __init__(self, pki: Pki):
        self.registros: list[dict] = []
        self.roteiro: dict[str, tuple[int, bytes, dict, float]] = {}      # caminho -> (status, corpo, cabecalhos, atraso_s)
        self.sse_abertos = 0
        self.sse_fechado = threading.Event()
        self._todos = threading.Event()
        self._fila: queue.Queue = queue.Queue()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(pki.servidor_cert, pki.servidor_chave)
        ctx.load_verify_locations(cafile=str(pki.ca_raiz.arquivo))      # como a borda real: o certificado do bridge vem da CA raiz
        ctx.verify_mode = ssl.CERT_OPTIONAL
        self._srv = make_server(HOST, 0, self._app, threaded=True, ssl_context=ctx)
        self.url = f"https://{HOST}:{self._srv.server_port}"
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self.encerrar_todos()
        self._srv.shutdown()
        self._srv.server_close()

    # ---- controle pelo teste
    def responder(self, caminho: str, status: int = 200, corpo: bytes = b'{"status": "ok"}', cabecalhos: dict | None = None,
                  atraso: float = 0.0) -> None:
        self.roteiro[caminho] = (status, corpo, cabecalhos or {"Content-Type": "application/json"}, atraso)

    def emitir(self, quadro: bytes) -> None:
        self._fila.put(quadro)

    def encerrar_sse(self) -> None:
        self._fila.put(FIM)

    def encerrar_todos(self) -> None:
        self._todos.set()

    def chamadas(self, caminho: str) -> list[dict]:
        return [r for r in self.registros if r["caminho"] == caminho]

    @staticmethod
    def _identidade(environ):
        """CN do certificado de cliente da conexao (o bridge se identificando), ou None."""
        pegar = getattr(environ.get("werkzeug.socket"), "getpeercert", None)
        cert = pegar() if pegar else None
        if not cert:
            return None
        return dict(x[0] for x in cert["subject"]).get("commonName")

    # ---- WSGI
    def _app(self, environ, start_response):
        corpo = environ["wsgi.input"].read(int(environ.get("CONTENT_LENGTH") or 0))
        cab = {k[5:].replace("_", "-").title(): v for k, v in environ.items() if k.startswith("HTTP_")}
        caminho = environ["PATH_INFO"]
        self.registros.append({"metodo": environ["REQUEST_METHOD"], "caminho": caminho, "cabecalhos": cab, "corpo": corpo,
                               "bridge": self._identidade(environ)})
        status, resposta, cabecalhos, atraso = self.roteiro.get(caminho, (200, b'{"status": "ok"}',
                                                                          {"Content-Type": "application/json"}, 0.0))
        if atraso:
            time.sleep(atraso)
        if caminho == "/push/wokwi" and status == 200:
            return self._sse(start_response, cab.get("Authorization", ""))
        start_response(f"{status} X", list(cabecalhos.items()) + [("Content-Length", str(len(resposta)))])
        return [resposta]

    def _sse(self, start_response, dono: str):
        start_response("200 OK", [("Content-Type", "text/event-stream"), ("Cache-Control", "no-cache")])
        fila: queue.Queue = queue.Queue()
        self.sse_abertos += 1
        self.sse_fechado.clear()

        def gerar():
            try:
                yield b": conectado\n\n"
                while True:
                    try:
                        item = self._fila.get(timeout=0.1)
                    except queue.Empty:
                        if self._todos.is_set():
                            return
                        continue
                    if item is FIM:
                        return
                    yield item
            finally:
                self.sse_abertos -= 1
                self.sse_fechado.set()
        return gerar()
