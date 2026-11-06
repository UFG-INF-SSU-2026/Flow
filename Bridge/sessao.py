"""
Sessao de UM dispositivo diante do servidor: repassa o login, renova o token, envia eventos cifrados e mantem o canal de push
(SSE), traduzindo cada mensagem em comando numa fila que a API entrega ao dispositivo.

O bridge nao autentica nem autoriza: o login do dispositivo e REPASSADO ao /auth/login e quem aceita ou recusa e o servidor.
O segredo passa por login() e e descartado; a sessao guarda so os tokens, em memoria. Se o refresh falhar, nao ha como
refazer o login sozinho: levanta Relogin e o dispositivo manda o login de novo.
"""
import json
import queue
import threading
from collections import deque

import requests

from config import Config
from crypto_utils import decrypt_payload, encrypt_payload
from traducao import MENSAGENS_VISTAS_MAX, comando_do_push, ler_eventos

TIMEOUT_S = 5
LEITURA_SSE_S = 40               # > keepalive do servidor (15 s): detecta conexao morta
BACKOFF_MAX_S = 30
COMANDOS_MAX = 50                # fila por dispositivo: se encher, o comando novo e descartado (o firmware tem prazo proprio)


class ErroServidor(Exception):
    """O servidor respondeu com um codigo de erro. O bridge nao interpreta: a API devolve o mesmo codigo."""

    def __init__(self, status: int):
        super().__init__(f"servidor respondeu {status}")
        self.status = status


class ServidorIndisponivel(Exception):
    """Sem resposta do servidor (rede, TLS ou timeout)."""


class Relogin(Exception):
    """A sessao nao pode ser renovada: o dispositivo precisa mandar o login de novo."""


def cifrar(obj: dict) -> dict:
    nonce, ct = encrypt_payload(json.dumps(obj))
    return {"nonce": nonce, "ciphertext": ct}


def decifrar(envelope: dict) -> dict:
    return json.loads(decrypt_payload(envelope["nonce"], envelope["ciphertext"]))


def log(msg: str) -> None:
    print(f"[bridge] {msg}", flush=True)


class Sessao:
    def __init__(self, device_id: str, cfg: Config):
        self.device_id = device_id
        self.cfg = cfg
        self._lock = threading.Lock()
        self.access: str | None = None
        self.refresh_token: str | None = None
        self.user_id: int | None = None
        self.comandos: queue.Queue = queue.Queue(maxsize=COMANDOS_MAX)
        self.vistas: deque = deque(maxlen=MENSAGENS_VISTAS_MAX)
        self.push_conectado = False
        self.precisa_login = False
        self.erros: deque = deque(maxlen=5)          # textos curtos, sem dados pessoais
        self._parar = threading.Event()

    # ------------------------------------------------------------------ servidor
    def _post(self, rota: str, corpo: dict, **kw):
        try:
            return requests.post(f"{self.cfg.servidor}{rota}", json=cifrar(corpo), timeout=TIMEOUT_S,
                                 verify=self.cfg.ca, **kw)
        except requests.RequestException as e:
            raise ServidorIndisponivel(type(e).__name__) from None

    def _aplicar(self, tokens: dict) -> None:
        self.access = tokens["access_token"]
        self.refresh_token = tokens["refresh_token"]
        self.user_id = tokens["user_ids"][0]          # o user_id vem do login, nao de configuracao
        self.precisa_login = False

    def login(self, identificador: str, segredo: str) -> None:
        """Repassa o login ao servidor. O segredo nao e guardado: so vive nesta chamada."""
        r = self._post("/auth/login", {"schemaVersion": 3, "identificador": identificador, "segredo": segredo,
                                       "fonte": "wokwi", "device_id": self.device_id})
        if r.status_code != 200:
            raise ErroServidor(r.status_code)
        self._aplicar(decifrar(r.json()))

    def renovar(self, token_recusado: str | None = None) -> None:
        """Refresh. token_recusado: o access token que acabou de receber 401; se outra thread ja o trocou, nao ha o que
        renovar (o refresh token e de uso unico). Refresh recusado (400, 401, 403) -> Relogin; servidor fora ou 5xx mantem
        os tokens (a renovacao e tentada de novo depois)."""
        with self._lock:
            if token_recusado is not None and self.access != token_recusado:
                return
            r = self._post("/auth/refresh", {"schemaVersion": 3, "refresh_token": self.refresh_token})
            if r.status_code == 200:
                self._aplicar(decifrar(r.json()))
            elif r.status_code in (400, 401, 403):
                self.precisa_login = True
                self.access = self.refresh_token = None
                raise Relogin()
            else:
                raise ErroServidor(r.status_code)

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access}"}

    def enviar(self, rota: str, evento: dict) -> None:
        """Envia o evento cifrado. Levanta ErroServidor (4xx/5xx), ServidorIndisponivel ou Relogin. Sem fila de
        retransmissao (decisao de arquitetura): falhou, o evento e descartado pelo chamador."""
        if self.access is None:
            raise Relogin()
        for tentativa in (1, 2):
            usado = self.access
            r = self._post(rota, evento, headers=self.headers())
            if r.status_code == 401 and tentativa == 1:
                self.renovar(usado)
                continue
            if r.status_code >= 300:
                raise ErroServidor(r.status_code)
            return

    # ------------------------------------------------------------------ push
    def iniciar_push(self) -> None:
        threading.Thread(target=self._laco_push, name=f"push-{self.device_id}", daemon=True).start()

    def parar(self) -> None:
        self._parar.set()
        self.push_conectado = False

    @property
    def ativa(self) -> bool:
        return not self._parar.is_set()

    def _entregar(self, msg: dict) -> None:
        cmd, motivo = comando_do_push(msg, self.user_id, self.vistas)
        if cmd is None:
            if motivo != "repetida":
                log(f"[{self.device_id}] push descartado: {motivo}")
            return
        try:
            self.comandos.put_nowait(cmd)
        except queue.Full:
            log(f"[{self.device_id}] fila de comandos cheia; comando descartado")

    def _laco_push(self) -> None:
        last_id, espera = 0, 1
        while self.ativa:
            try:
                h = {**self.headers(), "Accept": "text/event-stream"}
                usado = self.access
                if last_id:
                    h["Last-Event-ID"] = str(last_id)
                with requests.get(f"{self.cfg.servidor}/push/wokwi", headers=h, stream=True,
                                  timeout=(TIMEOUT_S, LEITURA_SSE_S), verify=self.cfg.ca) as r:
                    if r.status_code == 401:
                        self.renovar(usado)
                        self._parar.wait(1)               # evita laco quente se o servidor continuar recusando
                        continue
                    if r.status_code == 403:
                        self.erros.append("403 no canal de push")
                        self._parar.wait(BACKOFF_MAX_S)
                        continue
                    r.raise_for_status()
                    self.push_conectado = True
                    espera = 1
                    for ev in ler_eventos(r.iter_lines(decode_unicode=True)):
                        if not self.ativa:
                            break
                        if ev.get("event") == "push":
                            last_id = int(ev.get("id", last_id))
                            try:
                                self._entregar(decifrar(json.loads(ev["data"])))
                            except Exception as e:        # uma mensagem ruim nao derruba o canal
                                log(f"[{self.device_id}] push invalido descartado ({type(e).__name__})")
                        elif ev.get("event") == "reautenticar":
                            self.renovar(usado)
                            break
            except Relogin:
                self.erros.append("sessao expirada: o dispositivo precisa mandar o login de novo")
                break                                     # sem segredo, nao ha como refazer; o novo login reinicia tudo
            except (ServidorIndisponivel, ErroServidor, requests.RequestException, ValueError) as e:
                # Servidor fora do ar: zerar o Last-Event-ID e inofensivo (o contador do servidor parte do instante de
                # subida); o que ele reenviar cai em 'vistas' ou nao e enviado (expirado).
                last_id = 0
                self.erros.append(f"canal de push caiu ({type(e).__name__})")
            self.push_conectado = False
            self._parar.wait(espera)
            espera = min(espera * 2, BACKOFF_MAX_S)
        self.push_conectado = False

    def estado(self) -> dict:
        """Saude da sessao, sem tokens, segredos nem dados pessoais."""
        return {"deviceId": self.device_id, "logado": self.access is not None, "precisaLogin": self.precisa_login,
                "pushConectado": self.push_conectado, "comandosPendentes": self.comandos.qsize(),
                "erros": list(self.erros)}
