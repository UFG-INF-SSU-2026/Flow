"""Servidor (borda real, banco falso) e bridge juntos, em processo unico, em TLS, com CA e certificados temporarios.

Esta pasta e a unica que importa servidor e bridge ao mesmo tempo; as fronteiras entre os pacotes de producao ficam intactas."""
import importlib.util
import os
import sys
import tempfile
import threading
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
for pasta in (RAIZ / "SensorServer", RAIZ / "Bridge"):
    sys.path.insert(0, str(pasta))

os.environ["FLOW_SEM_DOTENV"] = "1"
os.environ["FLOW_JWT_SECRET"] = "t" * 48
os.environ["FLOW_SEM_THREADS"] = "1"
os.environ["FLOW_DB_FAKE"] = "1"

import pytest  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

import api  # noqa: E402  (Bridge)
import bridge  # noqa: E402  (Bridge)
from borda import auth, const as const_borda, emissao, server  # noqa: E402
from comum import tls  # noqa: E402
from config import Config  # noqa: E402  (Bridge)
from flow_db import repositorios  # noqa: E402

HOST = "127.0.0.1"


def _gerador():
    spec = importlib.util.spec_from_file_location("gerar_certificado", RAIZ / "SensorServer" / "scripts" / "gerar_certificado.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Ambiente:
    pass


@pytest.fixture
def ambiente(monkeypatch):
    """Borda real (TLS, CA de dispositivos, certificado de cliente EXIGIDO) + bridge real, em processo unico.
    Devolve um objeto com: ca (raiz), url_borda, url_bridge, porta_bridge, pasta, ca_disp (cert, chave)."""
    pasta = Path(tempfile.mkdtemp(prefix="flow_integrado_"))
    g = _gerador()
    ca, ca_chave = pasta / "ca.crt", pasta / "ca.key"
    g.criar_ca(ca, ca_chave, dias=2)
    disp, disp_chave = pasta / "ca-dispositivos.crt", pasta / "ca-dispositivos.key"
    g.criar_ca(disp, disp_chave, dias=30, nome=g.NOME_CA_DISPOSITIVOS)
    g.emitir(ca, ca_chave, pasta / "borda.crt", pasta / "borda.key", "borda", ["localhost"], [HOST], dias=2)
    # o que `gerar_certificado.py --nome bridge` emite: serverAuth + clientAuth, CN=bridge (a identidade do bridge na borda)
    g.emitir(ca, ca_chave, pasta / "bridge.crt", pasta / "bridge.key", "bridge", ["localhost"], [HOST], dias=2, cliente=True)
    for nome, valor in {"FLOW_TLS_CA": ca, "FLOW_TLS_CERT": pasta / "borda.crt", "FLOW_TLS_KEY": pasta / "borda.key",
                        "FLOW_CA_DISPOSITIVOS_CERT": disp, "FLOW_CA_DISPOSITIVOS_KEY": disp_chave}.items():
        monkeypatch.setenv(nome, str(valor))
    monkeypatch.setattr(const_borda, "SCRYPT_N", 2 ** 4)
    monkeypatch.setattr(const_borda, "KEEPALIVE_S", 0.3)
    monkeypatch.setitem(server.app.config, "EXIGIR_CERTIFICADO", True)       # como em producao (os testes do servidor o desligam)

    repositorios.usar_fake()
    server.HUB._conns.clear()
    server.HUB._anel.clear()
    server.HUB._contador.clear()
    auth.criar_conta("disp1", "dispositivo", [1], ["wokwi"], "senha-1", device_ids=["esp32-1"])
    auth.criar_conta("disp2", "dispositivo", [2], ["wokwi"], "senha-2", device_ids=["esp32-2"])
    auth.criar_conta("idoso1", "idoso", [1], ["app"], "senha-i")

    contexto = tls.contexto_servidor()
    emissao.configurar_tls_de_cliente(contexto)                               # a borda pede (opcional) e confere o certificado de cliente
    borda = make_server(HOST, 0, server.app, threaded=True, ssl_context=contexto)
    threading.Thread(target=borda.serve_forever, daemon=True).start()

    a = Ambiente()
    a.pasta, a.ca, a.ca_disp = pasta, str(ca), (disp, disp_chave)
    a.url_borda = f"https://{HOST}:{borda.server_port}"
    cfg = Config(servidor=a.url_borda, ca=str(ca), host=HOST, porta=0, api_cert=str(pasta / "bridge.crt"),
                 api_chave=str(pasta / "bridge.key"), ca_dispositivos=str(disp), max_simulacoes=4)
    assert bridge.verificar(cfg) is None
    ponte = bridge.criar_servidor(cfg, bridge.acesso_logado(api.criar_app(cfg)))
    ponte.prepare()
    threading.Thread(target=ponte.serve, daemon=True).start()
    a.porta_bridge = ponte.bind_addr[1]
    a.url_bridge = f"https://{HOST}:{a.porta_bridge}"
    try:
        yield a
    finally:
        server.HUB._conns.clear()
        ponte.stop()
        borda.shutdown()
        borda.server_close()
