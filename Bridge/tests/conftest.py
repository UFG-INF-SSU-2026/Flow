"""Os testes do bridge nunca leem o Bridge/.env real (a configuracao vem de cada teste)."""
import os

os.environ["FLOW_SEM_DOTENV"] = "1"

import pytest  # noqa: E402

from config import Config  # noqa: E402


@pytest.fixture
def cfg():
    return Config(servidor="https://servidor.teste", ca="ca-teste.crt", max_simulacoes=2)
