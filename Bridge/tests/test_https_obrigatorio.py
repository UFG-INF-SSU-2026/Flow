"""Toda ponta do bridge fala HTTPS: a configuracao recusa texto claro, e a API nao tem rota nem listener sem TLS."""
import pytest

import bridge
from config import Config, ler_config


def test_config_recusa_servidor_em_texto_claro():
    with pytest.raises(ValueError):
        Config(servidor="http://127.0.0.1:5000", ca="ca.crt")


def test_ler_config_encerra_com_mensagem_se_o_servidor_nao_e_https(monkeypatch):
    monkeypatch.setenv("FLOW_SERVIDOR", "http://127.0.0.1:5000")
    with pytest.raises(SystemExit) as e:
        ler_config()
    assert "https://" in str(e.value)


def test_ler_config_aceita_https(monkeypatch):
    monkeypatch.setenv("FLOW_SERVIDOR", "https://127.0.0.1:5000/")
    assert ler_config().servidor == "https://127.0.0.1:5000"


def test_contexto_tls_da_api_exige_tls_1_2_ou_superior(tmp_path):
    import ssl
    from pathlib import Path
    pasta = Path(__file__).resolve().parents[2] / "SensorServer" / "certs"
    if not (pasta / "bridge.crt").is_file():
        pytest.skip("certificados nao gerados (python scripts/gerar_certificado.py --nome bridge)")
    cfg = Config(servidor="https://127.0.0.1:5000", ca=str(pasta / "ca.crt"), api_cert=str(pasta / "bridge.crt"),
                 api_chave=str(pasta / "bridge.key"))
    assert bridge.contexto_tls(cfg).minimum_version == ssl.TLSVersion.TLSv1_2


def test_conexao_persistente_usa_http_1_1():
    assert bridge.ConexaoPersistente.protocol_version == "HTTP/1.1"
