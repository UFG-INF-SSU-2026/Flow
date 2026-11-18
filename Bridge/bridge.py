"""
Bridge: porta de entrada HTTPS dos dispositivos simples (ESP32, Arduino, Wokwi, simuladores) para o servidor.

O bridge nao conhece Wokwi nem placa: recebe eventos pela API local (api.py), repassa o login do dispositivo ao servidor,
carimba o que o dispositivo nao conhece e devolve os comandos do servidor, ja traduzidos. O ESP32 do Wokwi fala HTTPS direto
com esta API (sketch/transporte_wifi.h); nao ha serial. Dispositivos simulados em software usam adaptadores/software.py.

Configuracao: Bridge/.env (modelo: .env.example) ou variaveis de ambiente (ver config.py).
Pre-requisitos: servidor no ar, conta de papel 'dispositivo' (fonte 'wokwi') criada por dispositivo, e os certificados
gerados em SensorServer/ (python scripts/gerar_certificado.py --nome bridge).

Uso:
  pip install -r requirements.txt
  python bridge.py
"""
import ipaddress
import ssl
import sys
from datetime import datetime
from pathlib import Path

from cheroot import errors as erros_cheroot
from cheroot.ssl.builtin import BuiltinSSLAdapter
from cheroot.wsgi import Server

import config
from api import criar_app
from sessao import log

DOTENV_CARREGADO = config.carregar_dotenv(config.DOTENV_ARQUIVO)    # antes de ler as variaveis


# Uma conexao TLS serve varias chamadas (o ESP32 faz uma por segundo; refazer o handshake a cada uma custa ~3,2 s no ESP32
# simulado e para o laco do firmware). Ociosa por mais que isso, a conexao e fechada e a chamada seguinte paga um handshake.
CONEXAO_OCIOSA_S = 10

# O long-poll de /comandos e as chamadas em andamento seguram uma thread cada; conexao ociosa nao segura.
THREADS_MINIMAS = 16
THREADS_POR_DISPOSITIVO = 2
THREADS_FOLGA = 4


class AdaptadorSoTls(BuiltinSSLAdapter):
    """Texto claro na porta TLS e derrubado sem resposta. O cheroot tentaria responder 400 em texto claro, e isso nao pode
    acontecer (nem de erro). Depende de internos da biblioteca (NoSSLError, wrap): por isso a faixa estreita em requirements.txt."""

    def wrap(self, sock):
        try:
            return super().wrap(sock)
        except erros_cheroot.NoSSLError as e:
            raise erros_cheroot.FatalSSLAlert(*e.args) from e


def contexto_tls(cfg: config.Config) -> ssl.SSLContext:
    """TLS 1.2 ou superior, com o certificado da API emitido pela CA do projeto."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cfg.api_cert, cfg.api_chave)
    return ctx


def numero_de_threads(cfg: config.Config) -> int:
    return max(THREADS_MINIMAS, THREADS_POR_DISPOSITIVO * cfg.max_simulacoes + THREADS_FOLGA)


def criar_servidor(cfg: config.Config, app) -> Server:
    """Servidor WSGI com TLS do projeto (>= 1.2) e conexao persistente. Nao faz bind nem inicia nada."""
    servidor = Server((cfg.host, cfg.porta), app, numthreads=numero_de_threads(cfg), timeout=CONEXAO_OCIOSA_S)
    adaptador = AdaptadorSoTls(cfg.api_cert, cfg.api_chave)
    adaptador.context = contexto_tls(cfg)                 # o contexto que vale e o do projeto, nao o padrao do adaptador
    servidor.ssl_adapter = adaptador
    return servidor


def acesso_logado(app):
    """Camada WSGI fina: uma linha por requisicao, ao terminar a resposta (hora, metodo, caminho sem query, status).
    Nunca registra corpo nem cabecalho (em especial Authorization). Nao bufferiza a resposta e propaga o close()."""
    def camada(environ, start_response):
        status = [""]

        def sr(estado, cabecalhos, *args):
            status[0] = estado.split(" ", 1)[0]
            return start_response(estado, cabecalhos, *args)

        metodo, caminho = environ.get("REQUEST_METHOD", ""), environ.get("PATH_INFO", "")
        resposta = app(environ, sr)
        return _FimDeResposta(resposta, lambda: _registrar_acesso(metodo, caminho, status[0]))
    return camada


class _FimDeResposta:
    """Devolve o iteravel da aplicacao como veio; ao fechar, fecha o original e registra o acesso."""

    def __init__(self, resposta, ao_fechar):
        self._resposta, self._ao_fechar = resposta, ao_fechar

    def __iter__(self):
        return iter(self._resposta)

    def close(self):
        try:
            if hasattr(self._resposta, "close"):
                self._resposta.close()
        finally:
            self._ao_fechar()


def _registrar_acesso(metodo: str, caminho: str, status: str) -> None:
    try:
        agora = datetime.now()
        log(f"{agora:%H:%M:%S}.{agora.microsecond // 1000:03d} {metodo} {caminho} {status}")
    except Exception:                                     # o log nunca derruba a requisicao
        pass


def verificar(cfg: config.Config) -> str | None:
    """Motivo pelo qual o bridge nao pode subir, ou None."""
    if not cfg.servidor.lower().startswith("https://"):
        return f"FLOW_SERVIDOR precisa comecar com https:// (toda ponta fala TLS); recebido: {cfg.servidor}"
    for caminho, o_que in ((cfg.ca, "certificado da CA (FLOW_TLS_CA)"), (cfg.api_cert, "certificado da API (FLOW_BRIDGE_API_CERT)"),
                           (cfg.api_chave, "chave da API (FLOW_BRIDGE_API_KEY)")):
        if not Path(caminho).is_file():
            return (f"{o_que} nao encontrado: {caminho}. Gere em SensorServer/ com "
                    f"'python scripts/gerar_certificado.py --nome bridge'.")
    return None


def _so_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def main() -> None:
    cfg = config.ler_config()
    motivo = verificar(cfg)
    if motivo:
        sys.exit(motivo)
    log(f"Configuracao (.env): {config.DOTENV_ARQUIVO} "
        + ("carregado" if DOTENV_CARREGADO else "nao encontrado; usando so as variaveis de ambiente"))
    log(f"Servidor: {cfg.servidor} | API: https://{cfg.host}:{cfg.porta} | limite: {cfg.max_simulacoes} dispositivos")
    if not _so_loopback(cfg.host):
        log(f"AVISO: a API escuta fora do loopback ({cfg.host}); o certificado da API precisa ter esse endereco "
            f"(gerar_certificado.py --nome bridge --ip ...), e os dispositivos da rede so devem usar HTTPS.")
    servidor = criar_servidor(cfg, acesso_logado(criar_app(cfg)))
    servidor.prepare()                                    # bind: erro de porta em uso chega aqui, com a mensagem do sistema
    log("Pronto. Os dispositivos (firmware no Wokwi, adaptador em software) fazem login em POST /v1/login, sempre por HTTPS.")
    try:
        servidor.serve()
    finally:
        servidor.stop()                                   # sem stop() as threads do cheroot seguram o processo (inclusive no Ctrl+C)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Encerrado pelo usuario.")
        sys.exit(0)
