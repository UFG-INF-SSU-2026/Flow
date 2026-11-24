"""TLS: a configuracao, a analise servida em TLS dentro do teste e o transporte da borda para a analise, que so aceita https://.
A CA do projeto assina o certificado de quem serve; o cliente da borda e o banco confiam so na CA.
Os testes de integracao que montam o servidor (conftest) seguem sem TLS: a analise roda em memoria, sem rede."""
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


@pytest.mark.parametrize("url", ["http://127.0.0.1:5100", "HTTP://127.0.0.1:5100"])
def test_transporte_da_borda_recusa_url_http_no_argumento(monkeypatch, tmp_path, url):
    # CA inexistente: se o transporte lesse a CA antes de conferir a URL, o erro seria outro (citaria gerar_certificado).
    monkeypatch.setenv("FLOW_TLS_CA", str(tmp_path / "nao-existe-ca.crt"))
    with pytest.raises(tls.ConfigTls, match=r"FLOW_ANALISE_URL.*https://"):
        cliente_analise.transporte_http(url, TOKEN)


def test_transporte_da_borda_recusa_url_http_vinda_do_ambiente(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOW_TLS_CA", str(tmp_path / "nao-existe-ca.crt"))
    monkeypatch.setenv("FLOW_ANALISE_URL", "http://127.0.0.1:5100")
    with pytest.raises(tls.ConfigTls, match=r"FLOW_ANALISE_URL.*https://"):
        cliente_analise.transporte_http(token=TOKEN)


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


def test_borda_escuta_em_todas_as_interfaces_por_padrao(monkeypatch):
    monkeypatch.delenv("FLOW_BORDA_HOST", raising=False)
    monkeypatch.delenv("FLOW_BORDA_PORT", raising=False)
    try:
        importlib.reload(const_borda)
        assert const_borda.HOST == "0.0.0.0" and const_borda.PORT == 5000
    finally:
        monkeypatch.undo()
        importlib.reload(const_borda)


def test_a_borda_mostra_os_ips_de_rede_do_proprio_certificado(tmp_path):
    """Com 0.0.0.0, a borda imprime o IP que vai no app: os do certificado dela, sem os de loopback."""
    from _apoio import gerador_de_certificado
    from borda import servico
    g = gerador_de_certificado()
    ca, ca_chave, cert, chave = (tmp_path / n for n in ("ca.crt", "ca.key", "borda.crt", "borda.key"))
    g.criar_ca(ca, ca_chave, dias=2)
    g.emitir(ca, ca_chave, cert, chave, "borda", ["localhost"], ["127.0.0.1", "::1", "10.114.192.202"], dias=2)
    assert servico.enderecos_de_rede(str(cert)) == ["10.114.192.202"]
    assert servico.enderecos_de_rede(str(tmp_path / "nada.crt")) == []
