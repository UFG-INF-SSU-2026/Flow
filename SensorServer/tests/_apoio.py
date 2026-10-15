"""
Apoio dos testes (sem fixtures): clientes que montam envelopes cifrados como o app e o bridge fazem,
e um leitor de SSE para o servidor real.
"""
import json
import queue
import threading
import time

import requests

from crypto_utils import decrypt_payload, encrypt_payload

SEGREDO = "t" * 48   # FLOW_JWT_SECRET dos testes (>= 32 bytes)


def aguardar(condicao, timeout: float = 3.0, passo: float = 0.02) -> bool:
    """Repete `condicao()` ate ser verdadeira ou estourar o timeout. Devolve o ultimo resultado."""
    fim = time.monotonic() + timeout
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(passo)
    return bool(condicao())


class Cliente:
    """Cliente de teste: cifra o corpo como o app, decifra a resposta de /auth. Subclasses definem o transporte."""

    def _enviar(self, metodo: str, rota: str, corpo, headers: dict):
        raise NotImplementedError

    def _json(self, resp):
        raise NotImplementedError

    @staticmethod
    def _cabecalhos(token, extras=None) -> dict:
        h = {}
        if token:
            h["Authorization"] = f"Bearer {token}"
        h.update(extras or {})
        return h

    def post(self, rota: str, obj, token=None, headers=None):
        nonce, ct = encrypt_payload(json.dumps(obj))
        return self._enviar("POST", rota, {"nonce": nonce, "ciphertext": ct}, self._cabecalhos(token, headers))

    def post_cru(self, rota: str, corpo, token=None):
        """Corpo JSON como esta, sem cifrar (para testar envelope malformado)."""
        return self._enviar("POST", rota, corpo, self._cabecalhos(token))

    def get(self, rota: str, token=None, headers=None):
        return self._enviar("GET", rota, None, self._cabecalhos(token, headers))

    def corpo(self, resp) -> dict:
        """Decifra uma resposta cifrada ({nonce, ciphertext})."""
        d = self._json(resp)
        return json.loads(decrypt_payload(d["nonce"], d["ciphertext"]))

    def login(self, identificador: str, segredo: str, fonte, device_id=None) -> dict:
        r = self.post("/auth/login", {"schemaVersion": 3, "identificador": identificador, "segredo": segredo,
                                      "fonte": fonte, "device_id": device_id})
        assert r.status_code == 200, f"login de {identificador} falhou: {r.status_code}"
        return self.corpo(r)


class ClienteFlask(Cliente):
    """Usa o test_client do Flask (sem rede)."""

    def __init__(self, flask_client):
        self.c = flask_client

    def _enviar(self, metodo, rota, corpo, headers):
        return self.c.open(rota, method=metodo, json=corpo, headers=headers)

    def _json(self, resp):
        return resp.get_json()


class ClienteReal(Cliente):
    """Usa HTTP de verdade contra o servidor numa porta efemera."""

    def __init__(self, base: str):
        self.base = base

    def _enviar(self, metodo, rota, corpo, headers):
        return requests.request(metodo, self.base + rota, json=corpo, headers=headers, timeout=10)

    def _json(self, resp):
        return resp.json()

    def sse(self, rota: str, token, headers=None) -> "LeitorSSE":
        return LeitorSSE(self.base + rota, token, headers)


class LeitorSSE:
    """Abre um stream SSE e le os quadros numa thread. Comentarios (': ...') e eventos ficam numa fila."""

    def __init__(self, url: str, token, headers=None):
        cab = {"Authorization": f"Bearer {token}"}
        cab.update(headers or {})
        self.resp = requests.get(url, headers=cab, stream=True, timeout=(5, 30))
        self.status = self.resp.status_code
        self.headers = self.resp.headers
        self.ultimo_id: int | None = None
        self.encerrado = threading.Event()
        self._itens: queue.Queue = queue.Queue()
        threading.Thread(target=self._ler, daemon=True).start()

    # ---- leitura em segundo plano ----
    def _ler(self) -> None:
        quadro: list[str] = []
        try:
            self.resp.encoding = "utf-8"
            for linha in self.resp.iter_lines(chunk_size=1, decode_unicode=True):
                if linha:
                    quadro.append(linha)
                elif quadro:
                    self._itens.put(self._interpretar(quadro))
                    quadro = []
        except Exception:  # noqa: BLE001  (conexao fechada pelo teste ou pelo servidor)
            pass
        finally:
            self.encerrado.set()

    @staticmethod
    def _interpretar(quadro: list[str]):
        if all(l.startswith(":") for l in quadro):
            return ("comentario", " ".join(l[1:].strip() for l in quadro))
        campos = {}
        for l in quadro:
            nome, _, valor = l.partition(":")
            campos[nome] = valor.lstrip(" ")
        return ("evento", campos)

    # ---- API para os testes ----
    def _proximo(self, timeout: float):
        try:
            return self._itens.get(timeout=max(timeout, 0.01))
        except queue.Empty:
            raise TimeoutError(f"nada chegou do stream em {timeout:.1f} s") from None

    def esperar_comentario(self, texto: str, timeout: float = 3.0) -> None:
        fim = time.monotonic() + timeout
        while True:
            tipo, valor = self._proximo(fim - time.monotonic())
            if tipo == "comentario" and texto in valor:
                return

    def proximo_evento(self, timeout: float = 5.0) -> dict:
        """Proximo evento (pula comentarios). Devolve {'id':..., 'event':..., 'data':...}."""
        fim = time.monotonic() + timeout
        while True:
            tipo, valor = self._proximo(fim - time.monotonic())
            if tipo == "evento":
                return valor

    def proximo_push(self, timeout: float = 5.0) -> dict:
        """Proximo evento 'push', ja decifrado. Guarda o id SSE em self.ultimo_id."""
        ev = self.proximo_evento(timeout)
        assert ev.get("event") == "push", f"esperava event: push, veio {ev}"
        self.ultimo_id = int(ev["id"])
        envelope = json.loads(ev["data"])
        return json.loads(decrypt_payload(envelope["nonce"], envelope["ciphertext"]))

    def sem_push(self, janela: float) -> bool:
        """True se nenhum evento chegar dentro da janela (comentarios de keepalive nao contam)."""
        fim = time.monotonic() + janela
        while True:
            restante = fim - time.monotonic()
            if restante <= 0:
                return True
            try:
                tipo, _ = self._itens.get(timeout=restante)
            except queue.Empty:
                return True
            if tipo == "evento":
                return False

    def esperar_fim(self, timeout: float = 3.0) -> bool:
        return self.encerrado.wait(timeout)

    def fechar(self) -> None:
        try:
            self.resp.close()
        except Exception:  # noqa: BLE001
            pass
