"""TLS: so a configuracao. A CA do projeto assina o certificado de quem serve; o cliente da borda e o banco confiam so na CA.
Os testes de integracao seguem sem TLS (a analise roda em memoria, sem rede)."""
import importlib
import threading

import pytest
from werkzeug.serving import make_server

from _apoio import gerar_par_tls
from analise import api
from borda import cliente_analise
from borda import const as const_borda
from comum import tls
from flow_db import config

TOKEN = "t" * 24


def _usar(monkeypatch, ca, cert, chave):
    monkeypatch.setenv("FLOW_TLS_CA", ca)
    monkeypatch.setenv("FLOW_TLS_CERT", cert)
    monkeypatch.setenv("FLOW_TLS_KEY", chave)


def test_sem_certificado_ou_com_url_http_nada_sobe(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOW_TLS_CERT", str(tmp_path / "nao-existe.crt"))
    with pytest.raises(tls.ConfigTls, match="gerar_certificado"):
        tls.certificado()
    monkeypatch.setenv("FLOW_TLS_CA", str(tmp_path / "nao-existe-ca.crt"))
    with pytest.raises(tls.ConfigTls, match="gerar_certificado"):
        tls.ca()
    with pytest.raises(tls.ConfigTls, match="https://"):
        tls.exigir_https("http://127.0.0.1:5100", "FLOW_ANALISE_URL")
    monkeypatch.setenv("FLOW_DB_SSLMODE", "require")
    with pytest.raises(config.ConfigBanco, match="FLOW_DB_SSLMODE"):
        config.sslmode()


def test_ca_assina_o_servico_e_so_a_ca_e_confianca(monkeypatch, tmp_path):
    ca, cert, chave = gerar_par_tls(tmp_path)
    _usar(monkeypatch, ca, cert, chave)
    monkeypatch.setenv("FLOW_DB_PASSWORD", "Senha_ok-123")
    dsn = config.dsn()
    assert "sslmode=verify-full" in dsn and "sslrootcert" in dsn

    # A analise serve HTTPS com o certificado emitido pela CA; o cliente da borda confia so em ca.crt e em nenhuma outra CA.
    srv = make_server("127.0.0.1", 0, api.criar_app(api.Analise(), TOKEN), threaded=True,
                      ssl_context=tls.contexto_servidor())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"https://127.0.0.1:{srv.server_port}"
    try:
        assert cliente_analise.transporte_http(url, TOKEN, ca)("GET", "/saude", None, 3)["ok"] is True
        outra_ca, _, _ = gerar_par_tls(tmp_path / "outra")
        with pytest.raises(cliente_analise.AnaliseIndisponivel):
            cliente_analise.transporte_http(url, TOKEN, outra_ca)("GET", "/saude", None, 3)
    finally:
        srv.shutdown()
        srv.server_close()


def test_borda_escuta_so_no_loopback_por_padrao(monkeypatch):
    monkeypatch.delenv("FLOW_BORDA_HOST", raising=False)
    monkeypatch.delenv("FLOW_BORDA_PORT", raising=False)
    try:
        importlib.reload(const_borda)
        assert const_borda.HOST == "127.0.0.1" and const_borda.PORT == 5000
    finally:
        monkeypatch.undo()
        importlib.reload(const_borda)
