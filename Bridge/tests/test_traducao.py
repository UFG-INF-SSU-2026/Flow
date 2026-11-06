"""Traducao pura (sem rede, serial nem Flask): o que vira comando para o firmware e o que e descartado."""
import json
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone

import pytest

import traducao
from sessao import cifrar, decifrar

from apoio import TIPO_ENCERRADA, TIPO_SOLICITADA, USER_ID, _iso_z, _sem, encerramento, pedido  # noqa: E402


def entregar(msg, vistas=None, user_id=USER_ID):
    """O que o bridge entregaria ao dispositivo: [comando] ou []."""
    cmd, _ = traducao.comando_do_push(msg, user_id, vistas if vistas is not None else deque(maxlen=100))
    return [cmd] if cmd else []


# ------------------------------------------------------------------ traduzir
def test_traduzir_solicitada_gera_buzzer_on():
    msg = pedido()
    assert traducao.traduzir(msg) == f"CMD;BUZZER_ON;{msg['messageId']};5000"


@pytest.mark.parametrize("resultado", ["CONFIRMADO", "EXPIRADO", "CANCELADO"])
def test_traduzir_encerrada_gera_buzzer_off(resultado):
    msg = encerramento(resultado)
    assert traducao.traduzir(msg) == f"CMD;BUZZER_OFF;{msg['messageId']};{resultado}"


def test_traduzir_tipo_desconhecido_devolve_none():
    assert traducao.traduzir(pedido(messageType="outro.tipo", payload={})) is None


# ------------------------------------------------------------------ tratar_push: o que chega a serial
def test_push_valido_escreve_um_comando():
    msg = pedido()
    assert entregar(msg) == [f"CMD;BUZZER_ON;{msg['messageId']};5000"]


def test_encerramento_valido_escreve_buzzer_off():
    msg = encerramento("EXPIRADO")
    assert entregar(msg) == [f"CMD;BUZZER_OFF;{msg['messageId']};EXPIRADO"]


def test_user_id_diferente_nao_escreve():
    assert entregar(pedido(userId=USER_ID + 1)) == []


def test_message_id_repetido_escreve_uma_vez():
    msg, vistas = pedido(), deque(maxlen=100)
    assert len(entregar(msg, vistas)) + len(entregar(msg, vistas)) == 1


def test_mensagem_expirada_nao_escreve():
    passado = _iso_z(datetime.now(timezone.utc) - timedelta(seconds=1))
    assert entregar(pedido(expiresAt=passado)) == []


def test_tipo_nao_suportado_nao_escreve():
    assert entregar(pedido(messageType="outro.tipo", payload={})) == []


# ------------------------------------------------------------------ tratar_push: forma invalida nao vira linha serial
@pytest.mark.parametrize("message_id", [
    "abc;def",              # ';' separaria campos do comando
    "abc\ndef",             # quebra de linha partiria o comando em dois
    "abc\n",                # quebra no FIM (o '$' de um regex comum aceitaria)
    "abc\rdef",
    "abc def",
    "",
    "a" * 40,               # maior que o buffer do firmware (40 bytes, com o terminador)
    "ação",
    123,                    # nao e texto
    None,
])
def test_message_id_invalido_nao_escreve(message_id):
    assert entregar(pedido(messageId=message_id)) == []


def test_message_id_ausente_nao_escreve():
    assert entregar(_sem(pedido(), "messageId")) == []


@pytest.mark.parametrize("message_id", [str(uuid.uuid4()), "a", "a" * 39, "msg-123"])
def test_message_id_valido_passa(message_id):
    assert entregar(pedido(messageId=message_id)) == [f"CMD;BUZZER_ON;{message_id};5000"]


@pytest.mark.parametrize("timeout_ms", ["5000", True, False, 5000.5, 5000.0, None, [5000]])
def test_timeout_ms_que_nao_e_inteiro_nao_escreve(timeout_ms):
    assert entregar(pedido(payload={"timeoutMs": timeout_ms})) == []


def test_timeout_ms_ausente_nao_escreve():
    assert entregar(pedido(payload={"motivo": "x"})) == []


@pytest.mark.parametrize("timeout_ms", [0, -1, 999_999])
def test_faixa_do_timeout_ms_nao_e_filtrada(timeout_ms):
    """Quem decide a faixa e o firmware (push.ack REJEITADO); o bridge so repassa um inteiro."""
    msg = pedido(payload={"timeoutMs": timeout_ms})
    assert entregar(msg) == [f"CMD;BUZZER_ON;{msg['messageId']};{timeout_ms}"]


@pytest.mark.parametrize("payload", [None, [], "texto", 5])
def test_payload_que_nao_e_objeto_nao_escreve(payload):
    assert entregar(pedido(payload=payload)) == []
    assert entregar(encerramento(payload=payload)) == []


def test_payload_ausente_nao_escreve():
    assert entregar(_sem(pedido(), "payload")) == []


@pytest.mark.parametrize("resultado", ["QUALQUER", "confirmado", "CONFIRMADO;X", "CONFIRMADO\n", "", None, 1, ["CONFIRMADO"], {}])
def test_resultado_invalido_nao_escreve(resultado):
    assert entregar(pedido(messageType=TIPO_ENCERRADA, payload={"resultado": resultado})) == []


def test_resultado_ausente_nao_escreve():
    assert entregar(pedido(messageType=TIPO_ENCERRADA, payload={})) == []


@pytest.mark.parametrize("tipo", [None, 5, ["x"]])
def test_message_type_que_nao_e_texto_nao_escreve(tipo):
    assert entregar(pedido(messageType=tipo)) == []


def test_mensagem_invalida_nao_ocupa_a_lista_de_vistas():
    vistas = deque(maxlen=100)
    entregar(pedido(messageId="abc;def"), vistas)
    assert len(vistas) == 0




# ------------------------------------------------------------------ parser SSE
def test_ler_eventos_push_completo():
    linhas = ["id: 5", "event: push", 'data: {"nonce": "a:b", "ciphertext": "c"}', ""]
    assert list(traducao.ler_eventos(linhas)) == [
        {"id": "5", "event": "push", "data": '{"nonce": "a:b", "ciphertext": "c"}'}]


def test_ler_eventos_ignora_comentario_keepalive():
    assert list(traducao.ler_eventos([":keepalive", ""])) == []
    assert list(traducao.ler_eventos([": keepalive", "id: 1", "event: push", "data: x", ""])) == [
        {"id": "1", "event": "push", "data": "x"}]


def test_ler_eventos_reautenticar_nao_tem_data():
    assert list(traducao.ler_eventos(["event: reautenticar", ""])) == [{"event": "reautenticar"}]


def test_ler_eventos_varios_eventos_e_linhas_em_branco_repetidas():
    linhas = ["id: 1", "event: push", "data: a", "", "", "id: 2", "event: push", "data: b", ""]
    assert [e["id"] for e in traducao.ler_eventos(linhas)] == ["1", "2"]


def test_ler_eventos_evento_truncado_sem_linha_em_branco_nao_e_gerado():
    linhas = ["id: 1", "event: push", "data: a", "", "id: 2", "event: push", "data: b"]
    assert [e["id"] for e in traducao.ler_eventos(linhas)] == ["1"]


def test_do_sse_cifrado_ate_o_comando():
    """Ponta a ponta sem rede: linhas SSE -> decifra -> comando_do_push -> comando para o firmware."""
    msg = pedido()
    linhas = ["id: 7", "event: push", "data: " + json.dumps(cifrar(msg)), "", ":keepalive", ""]
    vistas, comandos = deque(maxlen=100), []
    for ev in traducao.ler_eventos(linhas):
        assert ev["event"] == "push"
        cmd, _ = traducao.comando_do_push(decifrar(json.loads(ev["data"])), USER_ID, vistas)
        comandos.append(cmd)
    assert comandos == [f"CMD;BUZZER_ON;{msg['messageId']};5000"]


def test_rota_do_evento():
    assert traducao.rota_do_evento({"eventType": "push.ack"}) == "/eventos"
    assert traducao.rota_do_evento({"eventType": "confirmacao.resposta"}) == "/eventos"
    assert traducao.rota_do_evento({"eventType": "imobilidade.leitura"}) == "/dados"
