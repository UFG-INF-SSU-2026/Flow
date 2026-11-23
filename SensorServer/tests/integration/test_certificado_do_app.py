"""O app (que fala direto com a borda) precisa do certificado emitido por /auth/certificado; o Wokwi (via bridge) nao.
Primeiro a ligacao na rota (identidade do certificado simulada), depois o fluxo inteiro com TLS de verdade."""
import threading
from datetime import datetime, timezone

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from werkzeug.serving import make_server

from _apoio import ClienteReal, ambiente_tls, criar_ca_dispositivos, montar_csr
from borda import emissao, server
from comum import tls

IDOSO = ("idoso1", "cel1")
OUTRO_DISPOSITIVO = ("idoso1", "cel2")
OUTRA_CONTA = ("idoso2", "cel1")


@pytest.fixture
def exigindo(monkeypatch):
    monkeypatch.setitem(server.app.config, "EXIGIR_CERTIFICADO", True)


def _com_certificado(monkeypatch, identidade):
    monkeypatch.setattr(emissao, "identidade_do_cliente", lambda environ: identidade)


class TestLigacaoNaRota:
    def test_app_sem_certificado_leva_401(self, cli, tokens, exigindo):
        r = cli.post("/eventos", {}, tokens["idoso"])
        assert r.status_code == 401 and r.get_json()["codigo"] == "certificado_ausente"
        assert r.headers["WWW-Authenticate"] == "Bearer"

    @pytest.mark.parametrize("identidade", [OUTRO_DISPOSITIVO, OUTRA_CONTA], ids=["outro_device", "outra_conta"])
    def test_certificado_de_outro_device_ou_conta_leva_403(self, cli, tokens, exigindo, monkeypatch, identidade):
        _com_certificado(monkeypatch, identidade)
        r = cli.post("/eventos", {}, tokens["idoso"])
        assert r.status_code == 403 and r.get_json()["codigo"] == "certificado_incompativel"

    def test_certificado_que_bate_com_o_token_passa(self, cli, tokens, exigindo, monkeypatch):
        _com_certificado(monkeypatch, IDOSO)
        assert cli.post("/eventos", {}, tokens["idoso"]).status_code == 400          # passou da autorizacao; corpo vazio

    def test_quem_ainda_nao_tem_certificado_consegue_emitir_e_logar(self, cli, tokens, exigindo, ca_dispositivos):
        assert cli.post("/auth/certificado", {"csr": montar_csr()}, tokens["idoso"]).status_code == 200
        assert cli.login("idoso1", "senha-idoso", "app", "cel1")["access_token"]

    def test_o_wokwi_nao_e_exigido_aqui_quem_confere_e_o_bridge(self, cli, tokens, exigindo):
        assert cli.post("/eventos", {}, tokens["dispositivo"]).status_code == 400

    def test_sem_a_ca_de_dispositivos_na_subida_nada_e_exigido(self, cli, tokens):
        assert cli.post("/eventos", {}, tokens["idoso"]).status_code == 400          # EXIGIR_CERTIFICADO desligado (padrao)


class ClienteTls(ClienteReal):
    """ClienteReal sobre HTTPS: confia so na CA do projeto e, se tiver, apresenta o certificado de cliente."""

    def __init__(self, base, ca, cert=None):
        super().__init__(base)
        self.ca, self.cert = ca, cert

    def _enviar(self, metodo, rota, corpo, headers):
        return requests.request(metodo, self.base + rota, json=corpo, headers=headers, verify=self.ca, cert=self.cert, timeout=10)


@pytest.fixture
def borda_tls(contas, ca_dispositivos, monkeypatch):
    env = ambiente_tls()
    for nome, valor in env.items():
        monkeypatch.setenv(nome, valor)
    contexto = tls.contexto_servidor()
    assert emissao.pedir_certificado_de_cliente(contexto)
    monkeypatch.setitem(server.app.config, "EXIGIR_CERTIFICADO", True)
    srv = make_server("127.0.0.1", 0, server.app, threaded=True, ssl_context=contexto)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"https://127.0.0.1:{srv.server_port}", env["FLOW_TLS_CA"]
    finally:
        srv.shutdown()
        srv.server_close()


def _emitir_para(base, ca, token, tmp_path, nome):
    """O cliente gera a chave, pede o certificado e guarda o par em arquivos; so o CSR sai dele."""
    chave = ec.generate_private_key(ec.SECP256R1())
    r = ClienteTls(base, ca).post("/auth/certificado", {"csr": montar_csr(chave)}, token)
    assert r.status_code == 200
    cert_arq, chave_arq = tmp_path / f"{nome}.crt", tmp_path / f"{nome}.key"
    cert_arq.write_text(ClienteTls(base, ca).corpo(r)["certificado"], encoding="ascii")
    chave_arq.write_bytes(chave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
    return str(cert_arq), str(chave_arq)


def test_fluxo_completo_do_app_comeca_sem_certificado_recebe_e_usa(borda_tls, tmp_path):
    base, ca = borda_tls
    sem_certificado = ClienteTls(base, ca)
    token = sem_certificado.login("idoso1", "senha-idoso", "app", "cel1")["access_token"]      # 1. sem certificado: login passa

    assert sem_certificado.post("/eventos", {}, token).status_code == 401                      # 1. e o resto ainda nao

    cert = _emitir_para(base, ca, token, tmp_path, "app")                                      # 2 e 3. servidor emite, cliente recebe
    com_certificado = ClienteTls(base, ca, cert)
    assert com_certificado.post("/eventos", {}, token).status_code == 400                      # 4. segue normal (passou da autorizacao)
    assert com_certificado.get("/estado/cel1", token).status_code == 403                       # (rota de admin: permissao, nao certificado)


def test_certificado_de_outra_conta_nao_serve_para_este_token(borda_tls, tmp_path):
    base, ca = borda_tls
    outro = ClienteTls(base, ca).login("idoso2", "senha-idoso2", "app", "cel9")["access_token"]
    cert_do_outro = _emitir_para(base, ca, outro, tmp_path, "outro")
    meu = ClienteTls(base, ca).login("idoso1", "senha-idoso", "app", "cel1")["access_token"]
    r = ClienteTls(base, ca, cert_do_outro).post("/eventos", {}, meu)
    assert r.status_code == 403 and r.json()["codigo"] == "certificado_incompativel"


def test_certificado_de_outra_ca_e_recusado_no_handshake(borda_tls, tmp_path, monkeypatch):
    base, ca = borda_tls
    token = ClienteTls(base, ca).login("idoso1", "senha-idoso", "app", "cel1")["access_token"]
    (tmp_path / "intrusa").mkdir()
    intrusa = criar_ca_dispositivos(tmp_path / "intrusa")
    monkeypatch.setenv("FLOW_CA_DISPOSITIVOS_CERT", str(intrusa.cert))        # a CA da borda e outra: so este certificado vem da intrusa
    monkeypatch.setenv("FLOW_CA_DISPOSITIVOS_KEY", str(intrusa.chave))
    chave = ec.generate_private_key(ec.SECP256R1())
    cert = tmp_path / "falso.crt"
    cert.write_text(emissao.emitir_certificado(montar_csr(chave), "idoso1", "cel1", datetime.now(timezone.utc)).certificado_pem,
                    encoding="ascii")
    (tmp_path / "falso.key").write_bytes(chave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                              serialization.NoEncryption()))
    with pytest.raises(requests.exceptions.RequestException):
        ClienteTls(base, ca, (str(cert), str(tmp_path / "falso.key"))).post("/eventos", {}, token)


def test_o_wokwi_sem_certificado_segue_pela_borda_o_bridge_e_quem_confere(borda_tls):
    base, ca = borda_tls
    cli = ClienteTls(base, ca)
    token = cli.login("bridge1", "senha-bridge", "wokwi", "bridge-dev")["access_token"]
    assert cli.post("/eventos", {}, token).status_code == 400
