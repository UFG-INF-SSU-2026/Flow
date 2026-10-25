"""TLS: so a configuracao. O certificado unico serve a analise, o cliente da borda o confere e o banco recebe verify-full.
Os testes de integracao seguem sem TLS (a analise roda em memoria, sem rede)."""
import threading

import pytest
from werkzeug.serving import make_server

from _apoio import gerar_par_tls
from analise import api
from borda import cliente_analise
from comum import tls
from flow_db import config

TOKEN = "t" * 24


def _usar(monkeypatch, cert, chave):
    monkeypatch.setenv("FLOW_TLS_CERT", cert)
    monkeypatch.setenv("FLOW_TLS_KEY", chave)


def test_sem_certificado_ou_com_url_http_nada_sobe(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOW_TLS_CERT", str(tmp_path / "nao-existe.crt"))
    with pytest.raises(tls.ConfigTls, match="gerar_certificado"):
        tls.certificado()
    with pytest.raises(tls.ConfigTls, match="https://"):
        tls.exigir_https("http://127.0.0.1:5100", "FLOW_ANALISE_URL")
    monkeypatch.setenv("FLOW_DB_SSLMODE", "require")
    with pytest.raises(config.ConfigBanco, match="FLOW_DB_SSLMODE"):
        config.sslmode()


def test_banco_confere_o_certificado_e_analise_serve_https(monkeypatch, tmp_path):
    cert, chave = gerar_par_tls(tmp_path)
    _usar(monkeypatch, cert, chave)
    monkeypatch.setenv("FLOW_DB_PASSWORD", "Senha_ok-123")
    dsn = config.dsn()
    assert "sslmode=verify-full" in dsn and "sslrootcert" in dsn

    # A analise serve HTTPS com o certificado; o cliente da borda confia nele e em nenhum outro.
    srv = make_server("127.0.0.1", 0, api.criar_app(api.Analise(), TOKEN), threaded=True,
                      ssl_context=tls.contexto_servidor())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"https://127.0.0.1:{srv.server_port}"
    try:
        assert cliente_analise.transporte_http(url, TOKEN, cert)("GET", "/saude", None, 3)["ok"] is True
        outro, _ = gerar_par_tls(tmp_path, "outro")
        with pytest.raises(cliente_analise.AnaliseIndisponivel):
            cliente_analise.transporte_http(url, TOKEN, outro)("GET", "/saude", None, 3)
    finally:
        srv.shutdown()
        srv.server_close()
