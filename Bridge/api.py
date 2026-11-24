"""
API HTTPS do bridge: repassador FINO entre o ESP32 (Wokwi) e a borda do servidor. Nao faz login, nao guarda token, nao cifra, nao
traduz, nao le o conteudo e NAO decide quem precisa de certificado: o firmware fala o protocolo da borda (envelope AES, login,
refresh, emissao do certificado) e o bridge so encaminha. Quem exige o certificado do dispositivo, por rota, e a BORDA.

Rotas (mesmo metodo e caminho da borda; qualquer outra coisa e 404 e nem chega a borda):

  POST /auth/login          POST /auth/refresh        POST /auth/certificado
  POST /dados               POST /eventos             GET  /push/wokwi   (SSE repassado como fluxo continuo)

Certificado do dispositivo (mTLS). O ESP32 comeca SEM certificado (login e emissao) e depois passa a apresentar o emitido por
POST /auth/certificado. O TLS do dispositivo termina aqui, e o handshake (bridge.py) pede o certificado de cliente de forma
OPCIONAL e o confere SO contra a CA de dispositivos: sem certificado conecta; de outra CA (inclusive a raiz), vencido ou so com
serverAuth derruba a conexao no handshake, sem resposta HTTP. Para a borda o bridge:

  1. se identifica em TODA chamada, inclusive na abertura do SSE, com o PROPRIO certificado (certs/bridge.crt, emitido pela CA
     raiz, com clientAuth e CN "bridge"): e assim que a borda sabe que o cabecalho abaixo e confiavel;
  2. APAGA sempre o cabecalho X-Flow-Certificado-Cliente que veio do dispositivo (sem isso, um ESP32 sem certificado poderia
     mandar o certificado, que e publico, de outro) e so entao, se o handshake do dispositivo trouxe certificado valido,
     acrescenta o verdadeiro: o DER dele em base64, sem quebras de linha.

A borda confere o certificado repassado de novo e o compara com o token (conta e device_id). O bridge nao extrai identidade do
certificado e nao liga certificado a token.

Erros do proprio bridge (JSON com status "erro" e mensagem): 404 rota desconhecida, 413 corpo acima de CORPO_MAX_BYTES, 502 borda
fora ou com erro 5xx, 504 borda lenta. 2xx e 4xx da borda voltam como vieram (inclusive WWW-Authenticate e o codigo, p.ex.
certificado_ausente e certificado_incompativel). Sem retentativa, sem fila, sem estado entre chamadas.

Log: uma linha por requisicao (bridge.py), sem corpo, sem Authorization, sem query string, sem o certificado.
"""
import base64
import threading
import time
from datetime import datetime

import requests
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from flask import Flask, Response, jsonify, request

from config import Config

CORPO_MAX_BYTES = 16 * 1024             # a telemetria e o maior corpo legitimo (poucas centenas de bytes cifrados)
TIMEOUT_S = 5                           # conexao e leitura de uma chamada comum a borda
LEITURA_SSE_S = 40                      # leitura parada no push: maior que o keepalive de 15 s da borda; detecta conexao morta
INTERVALO_LOG_FALHA_S = 10              # falha repetida contra a borda: no maximo uma linha de log neste intervalo

# Metodo e caminho exatos que o bridge repassa -> a resposta e um stream SSE. Sem curinga e sem normalizacao de caminho. Esta lista so
# limita o que chega a borda; nao decide nada sobre certificado (quem decide e a borda).
ROTAS: dict[tuple[str, str], bool] = {
    ("POST", "/auth/login"):       False,
    ("POST", "/auth/refresh"):     False,
    ("POST", "/auth/certificado"): False,
    ("POST", "/dados"):            False,
    ("POST", "/eventos"):          False,
    ("GET", "/push/wokwi"):        True,
}
# Combinado com a borda (SensorServer/borda/const.py, CABECALHO_CERTIFICADO_REPASSADO); testes_integrados/ confere que sao iguais.
CABECALHO_CERTIFICADO = "X-Flow-Certificado-Cliente"
CABECALHOS_DE_IDA = ("Authorization", "Content-Type", "Accept", "Last-Event-ID")
CABECALHOS_DE_VOLTA = ("Content-Type", "WWW-Authenticate", "Cache-Control", "Retry-After")

_lock_log = threading.Lock()
_ultimo_log_falha: dict[str, float] = {}      # unico estado do modulo: so limita a repeticao de uma linha de log


def log(mensagem: str) -> None:
    print(f"[bridge] {mensagem}", flush=True)


def _log_falha(categoria: str) -> None:
    agora = time.monotonic()
    with _lock_log:
        if agora - _ultimo_log_falha.get(categoria, -INTERVALO_LOG_FALHA_S) < INTERVALO_LOG_FALHA_S:
            return
        _ultimo_log_falha[categoria] = agora
    log(f"borda: {categoria} (repetido: no maximo uma linha a cada {INTERVALO_LOG_FALHA_S} s)")


def _erro(status: int, mensagem: str, **extra):
    return jsonify({"status": "erro", "mensagem": mensagem, **extra}), status


def cabecalho_do_certificado(environ) -> str | None:
    """O certificado de cliente que o dispositivo apresentou no handshake, como o valor do cabecalho de repasse (base64 do DER, sem
    quebras de linha), ou None se nao apresentou um certificado valido. Falha fechada: o que nao der para interpretar nao e repassado
    (a borda, sem o cabecalho, responde certificado_ausente)."""
    pem = environ.get("SSL_CLIENT_CERT")
    if environ.get("SSL_CLIENT_VERIFY") != "SUCCESS" or not pem:
        return None
    try:
        der = x509.load_pem_x509_certificate(pem.encode("ascii")).public_bytes(serialization.Encoding.DER)
    except Exception:                       # noqa: BLE001  (ilegivel: mesma resposta)
        return None
    return base64.b64encode(der).decode("ascii")


def _repasse_de_resposta(resp, mapa_cabecalhos=CABECALHOS_DE_VOLTA) -> Response:
    saida = Response(resp.content, status=resp.status_code)
    for nome in mapa_cabecalhos:
        if resp.headers.get(nome):
            saida.headers[nome] = resp.headers[nome]
    return saida


def criar_app(cfg: Config, chamar=requests.request) -> Flask:
    """`chamar` faz a chamada a borda (padrao: requests.request); so os testes unitarios o trocam."""
    app = Flask("bridge")
    app.config["MAX_CONTENT_LENGTH"] = CORPO_MAX_BYTES
    app.url_map.merge_slashes = False                      # sem normalizar caminho: o que nao esta na tabela e 404

    @app.errorhandler(413)
    def _grande(_):
        return _erro(413, "corpo grande demais")

    @app.errorhandler(404)
    def _nao_achou(_):
        return _erro(404, "rota desconhecida")

    @app.errorhandler(405)
    def _metodo(_):
        return _erro(404, "rota desconhecida")

    def _falha_da_borda(erro: Exception):
        if isinstance(erro, requests.Timeout):
            _log_falha("prazo estourado")
            return _erro(504, "borda lenta")
        _log_falha("indisponivel")
        return _erro(502, "borda indisponivel")

    def _enviar_a_borda(metodo: str, caminho: str, corpo: bytes, stream: bool):
        cabecalhos = {n: request.headers[n] for n in CABECALHOS_DE_IDA if n in request.headers}     # lista fechada: sem o de repasse
        repasse = cabecalho_do_certificado(request.environ)
        if repasse:
            cabecalhos[CABECALHO_CERTIFICADO] = repasse
        prazo = (TIMEOUT_S, LEITURA_SSE_S) if stream else (TIMEOUT_S, TIMEOUT_S)
        return chamar(metodo, cfg.servidor + caminho, data=corpo or None, headers=cabecalhos, verify=cfg.ca,
                      cert=(cfg.api_cert, cfg.api_chave),                  # o bridge se identifica na borda em TODA chamada (inclusive o SSE)
                      allow_redirects=False, timeout=prazo, stream=stream)

    def _fluxo(resp, inicio: float):
        """Devolve ao dispositivo, na hora, o que a borda emite. Qualquer fim (fim do stream, queda, leitura parada, saida do
        dispositivo) fecha a conexao com a borda."""
        try:
            for bloco in resp.iter_content(chunk_size=None):
                if bloco:
                    yield bloco
        except requests.RequestException:                  # queda ou leitura parada: o dispositivo reconecta com Last-Event-ID
            pass
        finally:
            resp.close()
            log(f"stream encerrado GET /push/wokwi apos {time.monotonic() - inicio:.1f} s")

    @app.route("/", defaults={"caminho": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    @app.route("/<path:caminho>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def repassar(caminho):
        regra = ROTAS.get((request.method, request.path))
        if regra is None:
            return _erro(404, "rota desconhecida")                       # a borda nao e tocada
        stream = regra
        corpo = request.get_data()                                       # 413 acima do limite (manipulador acima); nunca lido nem alterado
        inicio = time.monotonic()
        try:
            resp = _enviar_a_borda(request.method, request.path, corpo, stream)
        except requests.RequestException as e:
            return _falha_da_borda(e)
        if not (200 <= resp.status_code < 300 or 400 <= resp.status_code < 500):      # 5xx (e 3xx, inesperado): so o bridge responde
            resp.close()
            _log_falha(f"respondeu {resp.status_code}")
            return _erro(502, "borda com erro")
        if stream and resp.status_code == 200:
            log(f"stream aberto GET {request.path}")
            saida = Response(_fluxo(resp, inicio), status=200, mimetype="text/event-stream")
            saida.headers["Cache-Control"] = "no-cache"
            return saida
        try:
            return _repasse_de_resposta(resp)
        finally:
            resp.close()

    return app


def hora() -> str:
    agora = datetime.now()
    return f"{agora:%H:%M:%S}.{agora.microsecond // 1000:03d}"
