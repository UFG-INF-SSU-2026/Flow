"""Os testes do bridge nunca leem o Bridge/.env real (a configuracao vem de cada teste)."""
import os

os.environ["FLOW_SEM_DOTENV"] = "1"

import pytest  # noqa: E402

from config import Config  # noqa: E402


def pytest_collection_modifyitems(items):
    """Marca cada teste pela pasta (unit/integration), para `pytest -m unit` e `pytest -m integration`."""
    for item in items:
        partes = item.path.parts
        if "unit" in partes:
            item.add_marker("unit")
        elif "integration" in partes:
            item.add_marker("integration")


@pytest.fixture
def cfg():
    return Config(servidor="https://servidor.teste", ca="ca-teste.crt", ca_dispositivos="ca-disp-teste.crt", max_simulacoes=2)
