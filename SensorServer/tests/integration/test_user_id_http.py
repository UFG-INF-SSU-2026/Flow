"""user_id em /dados e /eventos: somente inteiro JSON nao booleano (P-01, R-02)."""
import time

import pytest

ROTAS = ["/dados", "/eventos"]


def _corpo(rota: str, user_id) -> dict:
    if rota == "/dados":
        corpo = {"source": "app", "device_id": "cel1", "timestamp_envio": int(time.time() * 1000),
                 "acelerometro": {"magnitude": 9.8, "magnitude_max_janela": 9.9}}
    else:
        corpo = {"source": "app", "device_id": "cel1", "event_time": "2026-10-14T12:00:00.000Z",
                 "eventType": "push.ack", "state": "ATIVADO", "messageId": "m1"}
    if user_id is not _AUSENTE:
        corpo["user_id"] = user_id
    return corpo


_AUSENTE = object()


@pytest.mark.parametrize("rota", ROTAS)
def test_inteiro_e_aceito(cli, tokens, rota):
    r = cli.post(rota, _corpo(rota, 1), tokens["idoso"])
    assert r.status_code == 200 and not r.get_json().get("descartado")


@pytest.mark.parametrize("rota", ROTAS)
@pytest.mark.parametrize("valor", ["1", " 1 ", 1.0, 1.9, True, False, None, [1], {"a": 1}, _AUSENTE],
                         ids=lambda v: "ausente" if v is _AUSENTE else repr(v))
def test_qualquer_outra_coisa_da_400(cli, tokens, rota, valor):
    r = cli.post(rota, _corpo(rota, valor), tokens["idoso"])
    assert r.status_code == 400
    assert "user_id" in r.get_json()["mensagem"]
