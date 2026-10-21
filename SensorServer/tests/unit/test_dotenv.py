"""Leitura do SensorServer/.env: o que vale, o que vence e o que nunca e lido nos testes."""
import pytest

from comum import const as const_comum


@pytest.fixture(autouse=True)
def _ligar_dotenv(monkeypatch):
    monkeypatch.delenv("FLOW_SEM_DOTENV", raising=False)   # conftest desliga; aqui a leitura e o objeto do teste


def _env(tmp_path, texto: str, **kw):
    p = tmp_path / ".env"
    p.write_bytes(texto.encode(kw.get("encoding", "utf-8")))
    return p


def test_le_as_variaveis_do_arquivo(tmp_path, monkeypatch):
    monkeypatch.delenv("FLOW_TESTE_A", raising=False)
    assert const_comum._carregar_dotenv(_env(tmp_path, "FLOW_TESTE_A=abc\n")) is True
    import os
    assert os.environ["FLOW_TESTE_A"] == "abc"
    monkeypatch.delenv("FLOW_TESTE_A")


def test_variavel_do_terminal_vence_o_arquivo(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOW_TESTE_B", "do-terminal")
    const_comum._carregar_dotenv(_env(tmp_path, "FLOW_TESTE_B=do-arquivo\n"))
    import os
    assert os.environ["FLOW_TESTE_B"] == "do-terminal"


def test_arquivo_ausente_nao_e_erro(tmp_path):
    assert const_comum._carregar_dotenv(tmp_path / ".env") is False


def test_bom_nao_estraga_a_primeira_chave(tmp_path, monkeypatch):
    monkeypatch.delenv("FLOW_TESTE_C", raising=False)
    const_comum._carregar_dotenv(_env(tmp_path, "FLOW_TESTE_C=ok\n", encoding="utf-8-sig"))
    import os
    assert os.environ.get("FLOW_TESTE_C") == "ok"
    monkeypatch.delenv("FLOW_TESTE_C")


def test_cifrao_e_cerquilha_entre_aspas_ficam_literais(tmp_path, monkeypatch):
    monkeypatch.delenv("FLOW_TESTE_D", raising=False)
    const_comum._carregar_dotenv(_env(tmp_path, 'FLOW_TESTE_D="a$b${X}#c"\n'))
    import os
    assert os.environ["FLOW_TESTE_D"] == "a$b${X}#c"
    monkeypatch.delenv("FLOW_TESTE_D")


def test_flow_sem_dotenv_desliga_a_leitura(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOW_SEM_DOTENV", "1")
    monkeypatch.delenv("FLOW_TESTE_E", raising=False)
    assert const_comum._carregar_dotenv(_env(tmp_path, "FLOW_TESTE_E=x\n")) is False
    import os
    assert "FLOW_TESTE_E" not in os.environ
