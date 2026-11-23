"""Todo cliente com fonte precisa do certificado emitido por /auth/certificado: o app o apresenta direto a borda; o wokwi o
apresenta ao bridge, que o repassa a borda num cabecalho, numa conexao autenticada com o certificado do proprio bridge.
Primeiro a ligacao na rota (identidade do certificado simulada), depois os fluxos inteiros com TLS de verdade."""
import base64
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from werkzeug.serving import make_server

from _apoio import ClienteReal, ambiente_tls, criar_ca_dispositivos, gerador_de_certificado, montar_csr
from borda import const as const_borda
from borda import emissao, server
from comum import tls

IDOSO = ("idoso1", "cel1")
WOKWI = ("bridge1", "bridge-dev")
OUTRO_DISPOSITIVO = ("idoso1", "cel2")
OUTRA_CONTA = ("idoso2", "cel1")


@pytest.fixture
def exigindo(monkeypatch):
    monkeypatch.setitem(server.app.config, "EXIGIR_CERTIFICADO", True)


def _com_certificado(monkeypatch, identidade):
    monkeypatch.setattr(emissao, "identidade_do_cliente", lambda environ, agora: identidade)


class TestLigacaoNaRota:
    @pytest.mark.parametrize("papel", ["idoso", "dispositivo"])
    def test_sem_certificado_leva_401(self, cli, tokens, exigindo, papel):
        r = cli.post("/eventos", {}, tokens[papel])
        assert r.status_code == 401 and r.get_json()["codigo"] == "certificado_ausente"
        assert r.headers["WWW-Authenticate"] == "Bearer"

    @pytest.mark.parametrize("identidade", [OUTRO_DISPOSITIVO, OUTRA_CONTA], ids=["outro_device", "outra_conta"])
    def test_certificado_de_outro_device_ou_conta_leva_403(self, cli, tokens, exigindo, monkeypatch, identidade):
        _com_certificado(monkeypatch, identidade)
        r = cli.post("/eventos", {}, tokens["idoso"])
        assert r.status_code == 403 and r.get_json()["codigo"] == "certificado_incompativel"

    @pytest.mark.parametrize("papel,identidade", [("idoso", IDOSO), ("dispositivo", WOKWI)])
    def test_certificado_que_bate_com_o_token_passa(self, cli, tokens, exigindo, monkeypatch, papel, identidade):
        _com_certificado(monkeypatch, identidade)
        assert cli.post("/eventos", {}, tokens[papel]).status_code == 400          # passou da autorizacao; corpo vazio

    @pytest.mark.parametrize("papel", ["idoso", "dispositivo"])
    def test_quem_ainda_nao_tem_certificado_consegue_emitir(self, cli, tokens, exigindo, ca_dispositivos, papel):
        assert cli.post("/auth/certificado", {"csr": montar_csr()}, tokens[papel]).status_code == 200

    def test_admin_sem_fonte_nao_precisa_de_certificado(self, cli, tokens, exigindo):
        assert cli.get("/estado/cel1", tokens["admin"]).status_code != 401

    def test_papel_com_fonte_sem_permissao_de_emitir_fica_bloqueado(self, cli, exigindo, monkeypatch):
        """Esquecer `certificado:emitir` num papel com fonte nao libera a rota sem certificado: bloqueia (fecha)."""
        from borda import authz
        monkeypatch.setitem(authz.POLITICA, "idoso", {"eventos:enviar"})
        token = cli.login("idoso1", "senha-idoso", "app", "cel1")["access_token"]
        assert cli.post("/eventos", {}, token).get_json()["codigo"] == "certificado_ausente"
        assert cli.post("/auth/certificado", {"csr": montar_csr()}, token).status_code == 403


# ------------------------------------------------------------------ TLS de verdade
class ClienteTls(ClienteReal):
    """ClienteReal sobre HTTPS: confia so na CA do projeto, apresenta o certificado de cliente que tiver e, se for o bridge,
    o cabecalho com o certificado repassado."""

    def __init__(self, base, ca, cert=None, extras=None):
        super().__init__(base)
        self.ca, self.cert, self.extras = ca, cert, extras or {}

    def _enviar(self, metodo, rota, corpo, headers):
        return requests.request(metodo, self.base + rota, json=corpo, headers={**headers, **self.extras}, verify=self.ca,
                                cert=self.cert, timeout=10)


@pytest.fixture
def borda_tls(contas, monkeypatch, tmp_path):
    env = ambiente_tls()
    for nome, valor in env.items():
        monkeypatch.setenv(nome, valor)
    contexto = tls.contexto_servidor()
    emissao.configurar_tls_de_cliente(contexto)
    monkeypatch.setitem(server.app.config, "EXIGIR_CERTIFICADO", True)
    srv = make_server("127.0.0.1", 0, server.app, threaded=True, ssl_context=contexto)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"https://127.0.0.1:{srv.server_port}", env["FLOW_TLS_CA"]
    finally:
        srv.shutdown()
        srv.server_close()


def _gravar_par(tmp_path, nome, cert_pem: str, chave) -> tuple[str, str]:
    cert_arq, chave_arq = tmp_path / f"{nome}.crt", tmp_path / f"{nome}.key"
    cert_arq.write_text(cert_pem, encoding="ascii")
    chave_arq.write_bytes(chave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
    return str(cert_arq), str(chave_arq)


def _emitir_para(base, ca, token, tmp_path, nome):
    """O cliente gera a chave, pede o certificado e guarda o par em arquivos; so o CSR sai dele."""
    chave = ec.generate_private_key(ec.SECP256R1())
    r = ClienteTls(base, ca).post("/auth/certificado", {"csr": montar_csr(chave)}, token)
    assert r.status_code == 200
    return _gravar_par(tmp_path, nome, ClienteTls(base, ca).corpo(r)["certificado"], chave)


def _certificado_do_bridge(tmp_path, nome=const_borda.NOME_CERTIFICADO_BRIDGE, cliente=True) -> tuple[str, str]:
    """O certificado que `gerar_certificado.py --nome bridge` emite, pela CA raiz de teste."""
    raiz = Path(ambiente_tls()["FLOW_TLS_CA"])
    cert, chave = tmp_path / f"{nome}-api.crt", tmp_path / f"{nome}-api.key"
    gerador_de_certificado().emitir(raiz, raiz.with_name("ca.key"), cert, chave, nome, ["localhost"], ["127.0.0.1"],
                                    dias=2, cliente=cliente)
    return str(cert), str(chave)


def _repassar(cert_arq: str) -> dict:
    """O que o bridge acrescenta: o certificado do dispositivo, em base64 do DER, no cabecalho combinado."""
    der = x509.load_pem_x509_certificate(Path(cert_arq).read_bytes()).public_bytes(serialization.Encoding.DER)
    return {const_borda.CABECALHO_CERTIFICADO_REPASSADO: base64.b64encode(der).decode("ascii")}


class TestApp:
    def test_fluxo_completo_comeca_sem_certificado_recebe_e_usa(self, borda_tls, tmp_path):
        base, ca = borda_tls
        sem_certificado = ClienteTls(base, ca)
        token = sem_certificado.login("idoso1", "senha-idoso", "app", "cel1")["access_token"]     # 1. login sem certificado
        assert sem_certificado.post("/eventos", {}, token).status_code == 401                     # 1. o resto ainda nao
        cert = _emitir_para(base, ca, token, tmp_path, "app")                                     # 2 e 3. emitido e recebido
        com_certificado = ClienteTls(base, ca, cert)
        assert com_certificado.post("/eventos", {}, token).status_code == 400                     # 4. segue normal

    def test_certificado_de_outra_conta_nao_serve_para_este_token(self, borda_tls, tmp_path):
        base, ca = borda_tls
        outro = ClienteTls(base, ca).login("idoso2", "senha-idoso2", "app", "cel9")["access_token"]
        cert_do_outro = _emitir_para(base, ca, outro, tmp_path, "outro")
        meu = ClienteTls(base, ca).login("idoso1", "senha-idoso", "app", "cel1")["access_token"]
        r = ClienteTls(base, ca, cert_do_outro).post("/eventos", {}, meu)
        assert r.status_code == 403 and r.json()["codigo"] == "certificado_incompativel"

    def test_certificado_de_outra_ca_e_recusado_no_handshake(self, borda_tls, tmp_path, monkeypatch):
        base, ca = borda_tls
        token = ClienteTls(base, ca).login("idoso1", "senha-idoso", "app", "cel1")["access_token"]
        (tmp_path / "intrusa").mkdir()
        intrusa = criar_ca_dispositivos(tmp_path / "intrusa")
        monkeypatch.setenv("FLOW_CA_DISPOSITIVOS_CERT", str(intrusa.cert))      # so este certificado vem da intrusa
        monkeypatch.setenv("FLOW_CA_DISPOSITIVOS_KEY", str(intrusa.chave))
        chave = ec.generate_private_key(ec.SECP256R1())
        falso = emissao.emitir_certificado(montar_csr(chave), "idoso1", "cel1", datetime.now(timezone.utc)).certificado_pem
        monkeypatch.setenv("FLOW_CA_DISPOSITIVOS_CERT", ambiente_tls()["FLOW_CA_DISPOSITIVOS_CERT"])
        with pytest.raises(requests.exceptions.RequestException):
            ClienteTls(base, ca, _gravar_par(tmp_path, "falso", falso, chave)).post("/eventos", {}, token)

    def test_o_sse_tambem_exige(self, borda_tls, tmp_path):
        base, ca = borda_tls
        token = ClienteTls(base, ca).login("idoso1", "senha-idoso", "app", "cel1")["access_token"]
        cab = {"Authorization": f"Bearer {token}"}
        sem = requests.get(base + "/push/app", headers=cab, verify=ca, stream=True, timeout=5)
        assert sem.status_code == 401
        sem.close()
        com = requests.get(base + "/push/app", headers=cab, verify=ca, cert=_emitir_para(base, ca, token, tmp_path, "app"),
                           stream=True, timeout=5)
        assert com.status_code == 200 and com.headers["Content-Type"].startswith("text/event-stream")
        com.close()


class TestWokwiPeloBridge:
    """O cliente de teste faz o papel do bridge: apresenta o certificado dele e repassa o do dispositivo."""

    @pytest.fixture
    def wokwi(self, borda_tls, tmp_path):
        base, ca = borda_tls
        token = ClienteTls(base, ca).login("bridge1", "senha-bridge", "wokwi", "bridge-dev")["access_token"]
        return base, ca, token, _emitir_para(base, ca, token, tmp_path, "esp32"), _certificado_do_bridge(tmp_path)

    def test_fluxo_completo_comeca_sem_certificado_recebe_e_usa_pelo_bridge(self, wokwi):
        base, ca, token, cert_esp32, cert_bridge = wokwi
        assert ClienteTls(base, ca, cert_bridge).post("/eventos", {}, token).status_code == 401      # bridge sem repasse
        r = ClienteTls(base, ca, cert_bridge, _repassar(cert_esp32[0])).post("/eventos", {}, token)
        assert r.status_code == 400                                                                 # passou da autorizacao
        sse = requests.get(base + "/push/wokwi", headers={"Authorization": f"Bearer {token}", **_repassar(cert_esp32[0])},
                           verify=ca, cert=cert_bridge, stream=True, timeout=5)
        assert sse.status_code == 200
        sse.close()

    def test_wokwi_direto_na_borda_sem_certificado_leva_401(self, wokwi):
        base, ca, token, _, _ = wokwi
        assert ClienteTls(base, ca).post("/eventos", {}, token).json()["codigo"] == "certificado_ausente"

    def test_repasse_vindo_de_quem_nao_e_o_bridge_e_ignorado(self, wokwi, tmp_path):
        """Certificado de dispositivo e publico (vai no handshake): mandar o de outro no cabecalho, direto, nao serve."""
        base, ca, token, cert_esp32, _ = wokwi
        assert ClienteTls(base, ca, None, _repassar(cert_esp32[0])).post("/eventos", {}, token).status_code == 401
        outro = ClienteTls(base, ca).login("idoso1", "senha-idoso", "app", "cel1")["access_token"]
        cert_app = _emitir_para(base, ca, outro, tmp_path, "app")
        r = ClienteTls(base, ca, cert_app, _repassar(cert_esp32[0])).post("/eventos", {}, token)
        assert r.json()["codigo"] == "certificado_incompativel"                  # vale o da conexao (o do app), nao o cabecalho

    def test_repasse_de_outra_conta_leva_403(self, wokwi, tmp_path):
        base, ca, token, _, cert_bridge = wokwi
        outro = ClienteTls(base, ca).login("idoso1", "senha-idoso", "app", "cel1")["access_token"]
        cert_app = _emitir_para(base, ca, outro, tmp_path, "app")
        r = ClienteTls(base, ca, cert_bridge, _repassar(cert_app[0])).post("/eventos", {}, token)
        assert r.json()["codigo"] == "certificado_incompativel"

    def test_repasse_invalido_leva_401(self, wokwi, tmp_path):
        base, ca, token, _, cert_bridge = wokwi
        ca_disp = ambiente_tls()["FLOW_CA_DISPOSITIVOS_CERT"]
        (tmp_path / "intrusa").mkdir()
        intrusa = criar_ca_dispositivos(tmp_path / "intrusa")
        chave_intrusa = serialization.load_pem_private_key(intrusa.chave.read_bytes(), None)
        chave_ca = serialization.load_pem_private_key(Path(ca_disp).with_suffix(".key").read_bytes(), None)
        emissor = x509.load_pem_x509_certificate(Path(ca_disp).read_bytes()).subject
        agora = datetime.now(timezone.utc)

        def assinar(chave_assina, inicio, fim, usos):
            b = (x509.CertificateBuilder().subject_name(x509.Name([
                    x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "bridge1"),
                    x509.NameAttribute(NameOID.COMMON_NAME, "bridge-dev")]))
                 .issuer_name(emissor).public_key(ec.generate_private_key(ec.SECP256R1()).public_key())
                 .serial_number(x509.random_serial_number()).not_valid_before(inicio).not_valid_after(fim))
            if usos:
                b = b.add_extension(x509.ExtendedKeyUsage(usos), critical=False)
            arq = tmp_path / f"c{x509.random_serial_number()}.crt"
            arq.write_bytes(b.sign(chave_assina, hashes.SHA256()).public_bytes(serialization.Encoding.PEM))
            return str(arq)

        casos = {
            "outra_ca": assinar(chave_intrusa, agora - timedelta(minutes=5), agora + timedelta(days=1),
                                [ExtendedKeyUsageOID.CLIENT_AUTH]),
            "vencido": assinar(chave_ca, agora - timedelta(days=8), agora - timedelta(days=1), [ExtendedKeyUsageOID.CLIENT_AUTH]),
            "sem_client_auth": assinar(chave_ca, agora - timedelta(minutes=5), agora + timedelta(days=1),
                                       [ExtendedKeyUsageOID.SERVER_AUTH]),
            "sem_uso_declarado": assinar(chave_ca, agora - timedelta(minutes=5), agora + timedelta(days=1), None),
        }
        for nome, arq in casos.items():
            r = ClienteTls(base, ca, cert_bridge, _repassar(arq)).post("/eventos", {}, token)
            assert r.status_code == 401, nome
        lixo = {const_borda.CABECALHO_CERTIFICADO_REPASSADO: "nao e base64 !"}
        assert ClienteTls(base, ca, cert_bridge, lixo).post("/eventos", {}, token).status_code == 401

    def test_certificado_da_raiz_que_nao_e_o_bridge_nao_repassa(self, wokwi, tmp_path):
        base, ca, token, cert_esp32, _ = wokwi
        impostor = _certificado_do_bridge(tmp_path, nome="outro-servico")
        assert ClienteTls(base, ca, impostor, _repassar(cert_esp32[0])).post("/eventos", {}, token).status_code == 401

    def test_certificado_do_bridge_sem_client_auth_e_recusado_no_handshake(self, wokwi, tmp_path):
        """O bridge.crt emitido antes (so serverAuth) precisa ser reemitido com --forcar."""
        base, ca, token, cert_esp32, _ = wokwi
        antigo = _certificado_do_bridge(tmp_path, cliente=False)
        with pytest.raises(requests.exceptions.RequestException):
            ClienteTls(base, ca, antigo, _repassar(cert_esp32[0])).post("/eventos", {}, token)
