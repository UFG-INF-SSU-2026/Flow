"""O bridge nao decide quem precisa de certificado (a borda decide); so repassa, quando o handshake trouxe um certificado valido,
o do dispositivo, no cabecalho combinado: base64 do DER, sem quebras de linha."""
import base64
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from api import CABECALHO_CERTIFICADO, cabecalho_do_certificado
from apoio import certificado_de_cliente, criar_ca


@pytest.fixture
def ca(tmp_path):
    return criar_ca(tmp_path, "ca-disp")


def _pem(caminho):
    return Path(caminho).read_text()


def _env(pem=None, verify="SUCCESS"):
    e = {"SSL_CLIENT_VERIFY": verify}
    if pem is not None:
        e["SSL_CLIENT_CERT"] = pem
    return e


def test_nome_do_cabecalho_e_o_combinado_com_a_borda():
    assert CABECALHO_CERTIFICADO == "X-Flow-Certificado-Cliente"


def test_valor_e_o_der_em_base64_sem_quebras(tmp_path, ca):
    crt, _ = certificado_de_cliente(tmp_path, ca)
    valor = cabecalho_do_certificado(_env(_pem(crt)))
    assert "\n" not in valor and " " not in valor and "BEGIN" not in valor
    esperado = x509.load_pem_x509_certificate(_pem(crt).encode()).public_bytes(serialization.Encoding.DER)
    assert base64.b64decode(valor, validate=True) == esperado


@pytest.mark.parametrize("env", [{}, {"SSL_CLIENT_VERIFY": "NONE"}, {"SSL_CLIENT_VERIFY": "SUCCESS"}, {"SSL_CLIENT_CERT": "x"},
                                 {"SSL_CLIENT_VERIFY": "FAILED:x", "SSL_CLIENT_CERT": "x"}])
def test_sem_certificado_valido_nao_ha_cabecalho(env):
    assert cabecalho_do_certificado(env) is None


@pytest.mark.parametrize("lixo", ["lixo", "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n", "\xff"])
def test_certificado_ilegivel_nao_e_repassado(lixo):
    assert cabecalho_do_certificado(_env(lixo)) is None
