"""POST /auth/certificado pelas rotas HTTP: login do dispositivo -> CSR -> certificado, resposta cifrada, recusas e log."""
import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from _apoio import csr_rsa, montar_csr
from comum import const as const_comum

ROTA = "/auth/certificado"


def _atributo(cert, oid):
    return cert.subject.get_attributes_for_oid(oid)[0].value


def test_i1_dispositivo_logado_recebe_o_certificado_cifrado(cli, tokens, ca_dispositivos):
    chave = ec.generate_private_key(ec.SECP256R1())
    r = cli.post(ROTA, {"csr": montar_csr(chave)}, tokens["dispositivo"])
    assert r.status_code == 200
    assert set(r.get_json()) == {"nonce", "ciphertext"}                   # nada em claro na resposta
    cert = x509.load_pem_x509_certificate(cli.corpo(r)["certificado"].encode("ascii"))
    assert _atributo(cert, NameOID.COMMON_NAME) == "bridge-dev" and _atributo(cert, NameOID.ORGANIZATIONAL_UNIT_NAME) == "bridge1"
    cert.verify_directly_issued_by(ca_dispositivos.certificado())
    assert cert.public_key().public_numbers() == chave.public_key().public_numbers()


def test_i2_campos_extras_do_pedido_sao_ignorados(cli, tokens, ca_dispositivos):
    r = cli.post(ROTA, {"csr": montar_csr(assunto="outro"), "device_id": "de-outro", "user_id": 2, "conta": "idoso2",
                        "schemaVersion": 1}, tokens["dispositivo"])
    assert r.status_code == 200
    cert = x509.load_pem_x509_certificate(cli.corpo(r)["certificado"].encode("ascii"))
    assert _atributo(cert, NameOID.COMMON_NAME) == "bridge-dev" and _atributo(cert, NameOID.ORGANIZATIONAL_UNIT_NAME) == "bridge1"


@pytest.mark.parametrize("papel", ["sem_token", "invalido"])
def test_i3_sem_token_ou_com_token_invalido_da_401(cli, tokens, ca_dispositivos, papel):
    r = cli.post(ROTA, {"csr": montar_csr()}, tokens[papel])
    assert r.status_code == 401 and r.headers["WWW-Authenticate"] == "Bearer"


def test_i4_admin_leva_403(cli, tokens, ca_dispositivos):
    r = cli.post(ROTA, {"csr": montar_csr()}, tokens["admin"])
    assert r.status_code == 403 and r.get_json()["codigo"] == "sem_permissao"


def test_i4_o_app_tambem_recebe_o_certificado_com_a_identidade_do_seu_token(cli, tokens, ca_dispositivos):
    r = cli.post(ROTA, {"csr": montar_csr(), "device_id": "de-outro"}, tokens["idoso"])
    assert r.status_code == 200
    cert = x509.load_pem_x509_certificate(cli.corpo(r)["certificado"].encode("ascii"))
    assert _atributo(cert, NameOID.COMMON_NAME) == "cel1" and _atributo(cert, NameOID.ORGANIZATIONAL_UNIT_NAME) == "idoso1"
    cert.verify_directly_issued_by(ca_dispositivos.certificado())


def test_a_versao_atual_e_a_ausente_sao_aceitas(cli, tokens, ca_dispositivos):
    assert const_comum.SCHEMA_VERSION_CERTIFICADO == 1
    for extra in ({}, {"schemaVersion": 1}):
        assert cli.post(ROTA, {"csr": montar_csr(), **extra}, tokens["dispositivo"]).status_code == 200


@pytest.mark.parametrize("versao", [2, 0, "1", True, None], ids=repr)
def test_versao_invalida_ou_maior_que_a_atual_da_400(cli, tokens, ca_dispositivos, versao):
    assert cli.post(ROTA, {"csr": montar_csr(), "schemaVersion": versao}, tokens["dispositivo"]).status_code == 400


def test_i5_csr_invalido_da_400_sem_eco_do_pedido(cli, tokens, ca_dispositivos):
    csr = csr_rsa()
    r = cli.post(ROTA, {"csr": csr}, tokens["dispositivo"])
    assert r.status_code == 400 and r.get_json()["codigo"] == "csr_invalido"
    texto = r.get_data(as_text=True)
    assert "BEGIN" not in texto and csr.splitlines()[1] not in texto


@pytest.mark.parametrize("corpo", [{}, {"csr": ""}, {"csr": 5}, {"schemaVersion": 3}], ids=repr)
def test_i6_corpo_fora_do_contrato_da_400(cli, tokens, ca_dispositivos, corpo):
    assert cli.post(ROTA, corpo, tokens["dispositivo"]).status_code == 400


def test_i6_corpo_que_nao_e_envelope_da_400(cli, tokens, ca_dispositivos):
    assert cli.post_cru(ROTA, {"csr": montar_csr()}, tokens["dispositivo"]).status_code == 400


def test_i7_sem_a_ca_de_dispositivos_da_503_e_o_resto_segue(cli, tokens, tmp_path, monkeypatch):
    monkeypatch.setenv("FLOW_CA_DISPOSITIVOS_CERT", str(tmp_path / "nada.crt"))
    monkeypatch.setenv("FLOW_CA_DISPOSITIVOS_KEY", str(tmp_path / "nada.key"))
    r = cli.post(ROTA, {"csr": montar_csr()}, tokens["dispositivo"])
    assert r.status_code == 503 and r.get_json()["codigo"] == "emissao_indisponivel"
    assert cli.get("/health").status_code == 200
    assert cli.login("bridge1", "senha-bridge", "wokwi", "bridge-dev")["access_token"]      # login e o resto continuam


def test_i8_o_log_tem_serial_conta_device_e_validade_e_nenhum_segredo(cli, tokens, ca_dispositivos, capsys):
    csr = montar_csr()
    r = cli.post(ROTA, {"csr": csr}, tokens["dispositivo"])
    certificado = cli.corpo(r)["certificado"]
    cert = x509.load_pem_x509_certificate(certificado.encode("ascii"))
    saida = capsys.readouterr().out
    assert (f"[certificado] emitido serial={format(cert.serial_number, 'x')} conta=bridge1 device_id=bridge-dev "
            f"valido_ate={cert.not_valid_after_utc.isoformat()}") in saida
    for nao in (csr.splitlines()[1], tokens["dispositivo"], certificado.splitlines()[1], "PRIVATE", "BEGIN"):
        assert nao not in saida
