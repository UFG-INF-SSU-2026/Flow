"""Stream SSE com servidor real: abertura, keepalive, cabecalhos, Last-Event-ID, dado cifrado e reautenticar."""
from borda import const as const_borda
import pytest

from borda import canal_app
from comum import const as const_comum
from analise import const as const_analise
from comum import mensagens


def _abrir(srv, headers=None):
    token = srv.login("idoso1", "senha-idoso", "app", "cel1")["access_token"]
    return srv.sse("/push/app", token, headers)


def test_abre_com_conectado_e_cabecalhos_corretos(servidor_real):
    leitor = _abrir(servidor_real)
    try:
        assert leitor.status == 200
        assert leitor.headers["Content-Type"].startswith("text/event-stream")
        assert leitor.headers["Cache-Control"] == "no-cache"
        assert leitor.headers["X-Accel-Buffering"] == "no"
        leitor.esperar_comentario("conectado")
    finally:
        leitor.fechar()


def test_keepalive_chega(servidor_real):
    leitor = _abrir(servidor_real)
    try:
        leitor.esperar_comentario("conectado")
        leitor.esperar_comentario("keepalive", timeout=3)    # KEEPALIVE_S = 0.3 na fixture
    finally:
        leitor.fechar()


@pytest.mark.parametrize("valor", ["abc", "-5", "3.5", "7"])
def test_last_event_id_invalido_nao_e_erro(servidor_real, valor):
    leitor = _abrir(servidor_real, {"Last-Event-ID": valor})
    try:
        assert leitor.status == 200
        leitor.esperar_comentario("conectado")
    finally:
        leitor.fechar()


def test_data_decifrada_e_o_json_do_contrato(servidor_real):
    leitor = _abrir(servidor_real)
    try:
        leitor.esperar_comentario("conectado")
        msg = mensagens.criar_pedido_confirmacao(1, "ep-1", const_comum.TIMEOUT_CONFIRMACAO_S, const_analise.MOTIVO_A)
        assert canal_app.enviar(1, msg) == "ENTREGUE_AO_CANAL"

        recebida = leitor.proximo_push()
        assert leitor.ultimo_id > 0
        assert recebida["schemaVersion"] == const_comum.SCHEMA_VERSION_MENSAGEM
        assert recebida["messageId"] == msg["messageId"]
        assert recebida["messageType"] == mensagens.TIPO_SOLICITADA
        assert recebida["userId"] == 1 and recebida["episodeId"] == "ep-1"
        assert recebida["expiresAt"] == msg["expiresAt"]
        assert recebida["payload"]["motivo"] == const_analise.MOTIVO_A
        # o replay/entrega recalcula timeoutMs (tempo que resta), entao fica um pouco abaixo do original
        assert 4000 <= recebida["payload"]["timeoutMs"] <= 5000
    finally:
        leitor.fechar()


def test_reautenticar_perto_do_vencimento_e_fecha_o_stream(servidor_real, monkeypatch):
    monkeypatch.setattr(const_borda, "ACCESS_TTL_S", 4)
    monkeypatch.setattr(const_borda, "AVISO_REAUTENTICAR_S", 3)    # avisa quando restar <= 3 s (em ate ~1 s)
    leitor = _abrir(servidor_real)
    try:
        leitor.esperar_comentario("conectado")
        ev = leitor.proximo_evento(timeout=5)
        assert ev["event"] == "reautenticar" and ev["data"] == "{}"
        assert leitor.esperar_fim(timeout=3)
    finally:
        leitor.fechar()
