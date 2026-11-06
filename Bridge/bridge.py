"""
Bridge: porta de entrada HTTPS dos dispositivos simples (ESP32, Arduino, Wokwi, simuladores) para o servidor.

O bridge nao conhece serial nem Wokwi: recebe eventos pela API local (api.py), repassa o login do dispositivo ao servidor,
carimba o que o dispositivo nao conhece e devolve os comandos do servidor, ja traduzidos. Quem fala com a serial ou simula
o dispositivo e um adaptador, em outro processo (adaptadores/).

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
from pathlib import Path

from werkzeug.serving import make_server

import config
from api import criar_app
from sessao import log

DOTENV_CARREGADO = config.carregar_dotenv(config.DOTENV_ARQUIVO)    # antes de ler as variaveis


def contexto_tls(cfg: config.Config) -> ssl.SSLContext:
    """TLS 1.2 ou superior, com o certificado da API emitido pela CA do projeto."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cfg.api_cert, cfg.api_chave)
    return ctx


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
    servidor = make_server(cfg.host, cfg.porta, criar_app(cfg), threaded=True, ssl_context=contexto_tls(cfg))
    log("Pronto. Os adaptadores e os dispositivos fazem login em POST /v1/login.")
    servidor.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Encerrado pelo usuario.")
        sys.exit(0)
