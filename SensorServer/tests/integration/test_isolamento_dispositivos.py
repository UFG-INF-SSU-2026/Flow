"""Uma conta nao age em nome do device de outra pessoa: device_id/deviceId do payload = device_id do token."""
import pytest

import auth
import regra_imobilidade
import const


@pytest.fixture
def bridge2(contas):
    auth.criar_conta("bridge2", "dispositivo", [2], ["wokwi"], "senha-bridge2")


def _wokwi(device, seq, user_id=1, **extra):
    return {"schemaVersion": const.SCHEMA_VERSION_WOKWI, "eventType": "imobilidade.leitura", "deviceId": device, "entityId": "e",
            "eventTimeMs": seq * 1000, "sequence": seq, "value": 0.3, "valueMax": 0.4, "unit": "m/s2",
            "state": "NORMAL", "user_id": user_id, "source": "wokwi", **extra}


def test_device_alheio_da_403_e_nao_altera_o_estado_dele(cli, tokens, bridge2):
    tok2 = cli.login("bridge2", "senha-bridge2", "wokwi", "bridge-2")["access_token"]

    # usuario 2 se passa pelo device do usuario 1 ("bridge-dev") com sequence altissima
    r = cli.post("/dados", _wokwi("bridge-dev", 10**9, user_id=2), tok2)
    assert r.status_code == 403 and r.get_json()["codigo"] == "fora_de_escopo"
    assert regra_imobilidade.obter_estado("bridge-dev") is None       # nada foi gravado para esse device

    # os eventos reais do usuario 1 continuam aceitos (nao viraram "duplicados")
    r = cli.post("/dados", _wokwi("bridge-dev", 1), tokens["dispositivo"])
    assert r.status_code == 200 and not r.get_json().get("descartado")
    estado = cli.get("/estado/bridge-dev", tokens["admin"]).get_json()
    assert estado["ultimo_sequence"] == 1


def test_o_proprio_device_e_aceito_para_o_usuario_2(cli, bridge2):
    tok2 = cli.login("bridge2", "senha-bridge2", "wokwi", "bridge-2")["access_token"]
    assert cli.post("/dados", _wokwi("bridge-2", 1, user_id=2), tok2).status_code == 200


@pytest.mark.parametrize("extra", [{"device_id": "outro"}, {"deviceId": "outro"}])
def test_qualquer_um_dos_dois_campos_divergente_da_403(cli, tokens, extra):
    ev = _wokwi("bridge-dev", 1)
    ev.update(extra)
    assert cli.post("/dados", ev, tokens["dispositivo"]).status_code == 403


def test_app_com_device_id_alheio_da_403(cli, tokens):
    dado = {"source": "app", "user_id": 1, "device_id": "celular-de-outro",
            "acelerometro": {"magnitude": 9.8, "magnitude_max_janela": 9.9}}
    r = cli.post("/dados", dado, tokens["idoso"])
    assert r.status_code == 403 and r.get_json()["codigo"] == "fora_de_escopo"
    assert cli.post("/dados", {**dado, "device_id": "cel1"}, tokens["idoso"]).status_code == 200


def test_eventos_com_device_alheio_da_403(cli, tokens):
    ev = {"schemaVersion": const.SCHEMA_VERSION_APP, "source": "app", "user_id": 1, "device_id": "celular-de-outro",
          "event_time": "2026-10-14T12:00:00.000Z", "eventType": "push.ack", "state": "ATIVADO", "messageId": "m"}
    assert cli.post("/eventos", ev, tokens["idoso"]).status_code == 403
    assert cli.post("/eventos", {**ev, "device_id": "cel1"}, tokens["idoso"]).status_code == 200


def test_sem_device_no_payload_nao_e_conferido(cli, tokens):
    dado = {"source": "app", "user_id": 1, "acelerometro": {"magnitude": 9.8, "magnitude_max_janela": 9.9}}
    assert cli.post("/dados", dado, tokens["idoso"]).status_code == 200


def test_403_por_device_vai_para_a_auditoria(cli, tokens):
    import json
    import const
    cli.post("/dados", _wokwi("outro", 1), tokens["dispositivo"])
    with open(f"{const.DATA_DIR}/auditoria.jsonl", encoding="utf-8") as f:
        (linha,) = [json.loads(l) for l in f if l.strip()]
    assert (linha["status"], linha["motivo"], linha["conta"]) == (403, "fora_de_escopo", "bridge1")


def test_mesmo_device_id_em_dois_usuarios_nao_compartilha_estado(cli, tokens, bridge2):
    tok2 = cli.login("bridge2", "senha-bridge2", "wokwi", "bridge-dev")["access_token"]   # mesmo device_id do usuario 1

    assert cli.post("/dados", _wokwi("bridge-dev", 50), tokens["dispositivo"]).status_code == 200
    # sequence 1 do usuario 2 nao e duplicado do sequence 50 do usuario 1
    assert cli.post("/dados", _wokwi("bridge-dev", 1, user_id=2), tok2).status_code == 200

    assert regra_imobilidade._estados[(1, "bridge-dev")].ultimo_sequence == 50
    assert regra_imobilidade._estados[(2, "bridge-dev")].ultimo_sequence == 1

    # o usuario 1 segue com o proprio sequence
    assert cli.post("/dados", _wokwi("bridge-dev", 51), tokens["dispositivo"]).status_code == 200
    assert regra_imobilidade._estados[(1, "bridge-dev")].ultimo_sequence == 51
    assert regra_imobilidade._estados[(2, "bridge-dev")].ultimo_sequence == 1


def test_estado_do_device_devolve_o_usuario_atualizado_por_ultimo(cli, tokens, bridge2):
    tok2 = cli.login("bridge2", "senha-bridge2", "wokwi", "bridge-dev")["access_token"]
    cli.post("/dados", _wokwi("bridge-dev", 7), tokens["dispositivo"])
    cli.post("/dados", _wokwi("bridge-dev", 3, user_id=2), tok2)
    estado = cli.get("/estado/bridge-dev", tokens["admin"]).get_json()
    assert (estado["user_id"], estado["ultimo_sequence"]) == (2, 3)

    cli.post("/dados", _wokwi("bridge-dev", 8), tokens["dispositivo"])
    estado = cli.get("/estado/bridge-dev", tokens["admin"]).get_json()
    assert (estado["user_id"], estado["ultimo_sequence"]) == (1, 8)
