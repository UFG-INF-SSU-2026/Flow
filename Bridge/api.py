"""
API local do bridge (HTTPS). Qualquer dispositivo simples a usa por um adaptador (serial, software) ou direto (ESP32 com WiFi).

  POST   /v1/login                           repassa o login ao servidor; devolve o token de sessao LOCAL
  POST   /v1/dispositivos/<id>/eventos       evento do dispositivo -> servidor (carimba user_id e source)
  GET    /v1/dispositivos/<id>/comandos      comandos ja traduzidos (?espera=<s>: espera ate esse tempo por um comando)
  GET    /v1/dispositivos/<id>/estado        saude da sessao, sem tokens nem dados pessoais
  DELETE /v1/dispositivos/<id>               encerra a sessao

Autenticacao e autorizacao sao do servidor: o bridge nao tem contas. O token local so amarra a chamada a sessao do
dispositivo que fez o login (sem ele, qualquer processo que alcance a API agiria em nome de um dispositivo ja logado). Nao e
credencial do servidor e nao vale fora daqui. O token de um dispositivo nas rotas de outro leva 403.

Erros: 401 sem token (ou {"codigo": "relogin"} quando a sessao nao pode ser renovada), 403 token de outro dispositivo,
400 evento invalido, 429 limite de dispositivos, 502 servidor indisponivel; um 4xx do servidor volta com o mesmo codigo.
"""
import hmac
import queue
import secrets
import threading

from flask import Flask, Response, jsonify, request

from config import Config
from sessao import ErroServidor, Relogin, ServidorIndisponivel, Sessao, log
from traducao import DEVICE_ID_RE, rota_do_evento

ESPERA_MAX_S = 25                # teto da espera por comando (o cliente nao fica pendurado)
CORPO_MAX_BYTES = 16 * 1024


class Dispositivos:
    """Sessoes ativas: device_id -> (Sessao, token local). Tudo em memoria."""

    def __init__(self, cfg: Config, criar_sessao=Sessao):
        self.cfg = cfg
        self._criar = criar_sessao
        self._lock = threading.Lock()
        self._itens: dict[str, tuple[Sessao, str]] = {}

    def cheio_para(self, device_id: str) -> bool:
        with self._lock:
            return device_id not in self._itens and len(self._itens) >= self.cfg.max_simulacoes

    def nova(self, device_id: str) -> Sessao:
        return self._criar(device_id, self.cfg)

    def registrar(self, sessao: Sessao) -> str:
        """Guarda a sessao ja logada (troca a anterior do mesmo dispositivo) e devolve o token local."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            antiga = self._itens.get(sessao.device_id)
            self._itens[sessao.device_id] = (sessao, token)
        if antiga is not None:
            antiga[0].parar()
        return token

    def dono_do_token(self, token: str) -> str | None:
        with self._lock:
            for device_id, (_, t) in self._itens.items():
                if hmac.compare_digest(t, token):
                    return device_id
        return None

    def sessao(self, device_id: str) -> Sessao | None:
        with self._lock:
            item = self._itens.get(device_id)
        return item[0] if item else None

    def remover(self, device_id: str) -> None:
        with self._lock:
            item = self._itens.pop(device_id, None)
        if item is not None:
            item[0].parar()


def _erro(status: int, mensagem: str, **extra):
    return jsonify({"status": "erro", "mensagem": mensagem, **extra}), status


def _texto(corpo: dict, campo: str) -> str | None:
    v = corpo.get(campo)
    return v if isinstance(v, str) and v else None


def criar_app(cfg: Config, criar_sessao=Sessao) -> Flask:
    app = Flask("bridge")
    app.config["MAX_CONTENT_LENGTH"] = CORPO_MAX_BYTES
    reg = Dispositivos(cfg, criar_sessao)
    app.extensions["dispositivos"] = reg

    def autorizar(device_id: str):
        """(sessao, None) se o token local pertence a este dispositivo; senao (None, resposta de erro)."""
        cab = request.headers.get("Authorization", "")
        token = cab[7:] if cab.startswith("Bearer ") else ""
        dono = reg.dono_do_token(token) if token else None
        if dono is None:
            return None, _erro(401, "token de sessao ausente ou invalido")
        if dono != device_id:
            return None, _erro(403, "token de outro dispositivo")
        return reg.sessao(device_id), None

    @app.errorhandler(413)
    def _grande(_):
        return _erro(400, "corpo grande demais")

    @app.errorhandler(404)
    def _nao_achou(_):
        return _erro(404, "rota desconhecida")

    @app.post("/v1/login")
    def login():
        corpo = request.get_json(silent=True)
        if not isinstance(corpo, dict):
            return _erro(400, "corpo precisa ser um objeto JSON")
        ident, segredo, device_id = (_texto(corpo, "identificador"), _texto(corpo, "segredo"), _texto(corpo, "deviceId"))
        if not (ident and segredo and device_id) or DEVICE_ID_RE.fullmatch(device_id) is None:
            return _erro(400, "login precisa de identificador, segredo e deviceId validos")
        if reg.cheio_para(device_id):
            return _erro(429, "limite de dispositivos atingido")
        sessao = reg.nova(device_id)
        try:
            sessao.login(ident, segredo)                  # repassa; o segredo nao e guardado em lugar nenhum
        except ErroServidor as e:
            return _erro(e.status if 400 <= e.status < 500 else 502, f"servidor recusou o login ({e.status})")
        except ServidorIndisponivel:
            return _erro(502, "servidor indisponivel")
        token = reg.registrar(sessao)
        sessao.iniciar_push()
        log(f"[{device_id}] login aceito pelo servidor (user_id={sessao.user_id})")
        return jsonify({"token": token, "deviceId": device_id, "userId": sessao.user_id})

    @app.post("/v1/dispositivos/<device_id>/eventos")
    def eventos(device_id):
        sessao, erro = autorizar(device_id)
        if erro:
            return erro
        evento = request.get_json(silent=True)
        if not isinstance(evento, dict) or not isinstance(evento.get("eventType"), str):
            return _erro(400, "evento precisa ser um objeto JSON com eventType")
        # O bridge so carimba o que o dispositivo nao conhece. O deviceId e do dispositivo: quem o confere com o do token e o
        # servidor (403 se diferir).
        evento["user_id"] = sessao.user_id
        evento["source"] = "wokwi"
        rota = rota_do_evento(evento)
        try:
            sessao.enviar(rota, evento)
        except Relogin:
            return _erro(401, "sessao expirada: mande o login de novo", codigo="relogin")
        except ErroServidor as e:
            log(f"[{device_id}] servidor respondeu {e.status} em {rota}")
            return _erro(e.status if 400 <= e.status < 500 else 502, f"servidor respondeu {e.status}")
        except ServidorIndisponivel:
            log(f"[{device_id}] servidor indisponivel em {rota}; evento descartado")
            return _erro(502, "servidor indisponivel")
        return jsonify({"status": "ok"})

    @app.get("/v1/dispositivos/<device_id>/comandos")
    def comandos(device_id):
        sessao, erro = autorizar(device_id)
        if erro:
            return erro
        try:
            espera = min(max(float(request.args.get("espera", "0")), 0.0), ESPERA_MAX_S)
        except ValueError:
            return _erro(400, "espera precisa ser um numero")
        lista: list[str] = []
        try:
            lista.append(sessao.comandos.get(timeout=espera) if espera else sessao.comandos.get_nowait())
            while True:
                lista.append(sessao.comandos.get_nowait())
        except queue.Empty:
            pass
        return jsonify({"comandos": lista})

    @app.get("/v1/dispositivos/<device_id>/estado")
    def estado(device_id):
        sessao, erro = autorizar(device_id)
        return erro if erro else jsonify(sessao.estado())

    @app.delete("/v1/dispositivos/<device_id>")
    def encerrar(device_id):
        _, erro = autorizar(device_id)
        if erro:
            return erro
        reg.remover(device_id)
        return Response(status=204)

    return app
