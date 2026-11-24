"""Bridge real (cheroot, TLS, em thread, porta 0) contra a borda falsa, tudo em loopback com PKI de teste."""
import pytest

from apoio import Pki, certificado_de_cliente, criar_pki
from ligacao import subir_bridge
from servidor_falso import BordaFalsa


@pytest.fixture
def pki(tmp_path) -> Pki:
    return criar_pki(tmp_path)


@pytest.fixture
def borda(pki):
    with BordaFalsa(pki) as b:
        yield b


@pytest.fixture
def ponte(pki, borda):
    servidor, porta, cfg = subir_bridge(pki, borda.url)
    try:
        yield porta
    finally:
        borda.encerrar_todos()               # streams abertos terminam (senao o stop esperaria o prazo de leitura)
        servidor.stop()


@pytest.fixture
def cliente_valido(pki, tmp_path):
    return certificado_de_cliente(tmp_path, pki.ca_disp)
