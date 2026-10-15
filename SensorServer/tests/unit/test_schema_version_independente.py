"""As versoes de schema sao independentes: subir uma constante nao arrasta as outras."""
import time
from datetime import datetime, timezone

import const
import mensagens


def _app_dado(**extra) -> dict:
    corpo = {"source": "app", "user_id": 1, "device_id": "cel1", "timestamp_envio": int(time.time() * 1000),
             "acelerometro": {"magnitude": 9.8, "magnitude_max_janela": 9.9}}
    corpo.update(extra)
    return corpo


def _login(cli, **extra):
    corpo = {"identificador": "idoso1", "segredo": "senha-idoso", "fonte": "app", "device_id": "cel1", **extra}
    return cli.post("/auth/login", corpo)


def test_push_segue_so_a_versao_de_mensagem(monkeypatch):
    for nome in ("SCHEMA_VERSION_APP", "SCHEMA_VERSION_WOKWI", "SCHEMA_VERSION_AUTH"):
        monkeypatch.setattr(const, nome, 9)
    monkeypatch.setattr(const, "SCHEMA_VERSION_MENSAGEM", 4)
    agora = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
    assert mensagens.criar_pedido_confirmacao(1, "ep1", 5, "X", agora=agora)["schemaVersion"] == 4
    resultado = sorted(mensagens.RESULTADOS_ENCERRAMENTO)[0]
    assert mensagens.criar_encerramento(1, "ep1", resultado, agora=agora)["schemaVersion"] == 4


def test_subir_wokwi_nao_afeta_app_em_dados(cli, tokens, monkeypatch):
    monkeypatch.setattr(const, "SCHEMA_VERSION_WOKWI", const.SCHEMA_VERSION_WOKWI + 1)
    r = cli.post("/dados", _app_dado(schemaVersion=const.SCHEMA_VERSION_APP + 1), tokens["idoso"])
    assert r.status_code == 200 and r.get_json().get("descartado") is True


def test_subir_app_nao_afeta_auth(cli, monkeypatch):
    monkeypatch.setattr(const, "SCHEMA_VERSION_APP", const.SCHEMA_VERSION_AUTH + 1)
    assert _login(cli, schemaVersion=const.SCHEMA_VERSION_AUTH + 1).status_code == 400
    assert _login(cli, schemaVersion=const.SCHEMA_VERSION_AUTH).status_code == 200


def test_400_do_login_mostra_a_versao_do_auth(cli, monkeypatch):
    monkeypatch.setattr(const, "SCHEMA_VERSION_AUTH", 5)
    r = _login(cli, schemaVersion=6)
    assert r.status_code == 400 and "maximo 5" in r.get_json()["mensagem"]
