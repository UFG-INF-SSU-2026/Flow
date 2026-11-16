"""Cliente da API do bridge (usado pelo adaptador em software): so fala HTTPS."""
import pytest

from adaptadores.cliente import ApiBridge


def test_cliente_da_api_recusa_http():
    with pytest.raises(ValueError):
        ApiBridge("http://127.0.0.1:5443", "ca.crt")


def test_cliente_da_api_aceita_https():
    assert ApiBridge("https://127.0.0.1:5443", "ca.crt").base == "https://127.0.0.1:5443"
