"""Leitura do Bridge/.env: o que vale, o que vence e o que nunca e lido nos testes."""
import os

import pytest

import config


@pytest.fixture(autouse=True)
def _ligar_dotenv(monkeypatch):
    monkeypatch.delenv("FLOW_SEM_DOTENV", raising=False)   # conftest desliga; aqui a leitura e o objeto do teste


def _env(tmp_path, texto: str, encoding="utf-8"):
    p = tmp_path / ".env"
    p.write_bytes(texto.encode(encoding))
    return p


def test_le_as_variaveis_do_arquivo(tmp_path, monkeypatch):
    monkeypatch.delenv("FLOW_TESTE_A", raising=False)
    assert config.carregar_dotenv(_env(tmp_path, "FLOW_TESTE_A=abc\n")) is True
    assert os.environ["FLOW_TESTE_A"] == "abc"
    monkeypatch.delenv("FLOW_TESTE_A")


def test_variavel_do_terminal_vence_o_arquivo(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOW_TESTE_B", "do-terminal")
    config.carregar_dotenv(_env(tmp_path, "FLOW_TESTE_B=do-arquivo\n"))
    assert os.environ["FLOW_TESTE_B"] == "do-terminal"


def test_arquivo_ausente_nao_e_erro(tmp_path):
    assert config.carregar_dotenv(tmp_path / ".env") is False


def test_bom_nao_estraga_a_primeira_chave(tmp_path, monkeypatch):
    monkeypatch.delenv("FLOW_TESTE_C", raising=False)
    config.carregar_dotenv(_env(tmp_path, "FLOW_TESTE_C=ok\n", encoding="utf-8-sig"))
    assert os.environ.get("FLOW_TESTE_C") == "ok"
    monkeypatch.delenv("FLOW_TESTE_C")


def test_senha_com_cifrao_e_cerquilha_entre_aspas_fica_literal(tmp_path, monkeypatch):
    monkeypatch.delenv("FLOW_TESTE_D", raising=False)
    config.carregar_dotenv(_env(tmp_path, 'FLOW_TESTE_D="s$3nh@#1"\n'))
    assert os.environ["FLOW_TESTE_D"] == "s$3nh@#1"
    monkeypatch.delenv("FLOW_TESTE_D")


def test_flow_sem_dotenv_desliga_a_leitura(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOW_SEM_DOTENV", "1")
    monkeypatch.delenv("FLOW_TESTE_E", raising=False)
    assert config.carregar_dotenv(_env(tmp_path, "FLOW_TESTE_E=x\n")) is False
    assert "FLOW_TESTE_E" not in os.environ


def test_ler_config_usa_padroes_e_valida_numeros(monkeypatch):
    for v in ("FLOW_SERVIDOR", "FLOW_BRIDGE_HOST", "FLOW_BRIDGE_PORT", "FLOW_BRIDGE_MAX_SIMULACOES", "FLOW_TLS_CA"):
        monkeypatch.delenv(v, raising=False)
    cfg = config.ler_config()
    assert (cfg.servidor, cfg.host, cfg.porta, cfg.max_simulacoes) == ("https://127.0.0.1:5000", "127.0.0.1", 5443, 8)
    assert cfg.ca.endswith("ca.crt") and cfg.api_cert.endswith("bridge.crt") and cfg.api_chave.endswith("bridge.key")
    monkeypatch.setenv("FLOW_BRIDGE_PORT", "abc")
    with pytest.raises(SystemExit):
        config.ler_config()
