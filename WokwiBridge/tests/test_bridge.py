"""
Testes do bridge (pytest), sem rede e sem serial.

Cobre o que o bridge faz com a mensagem de push (validar, descartar, traduzir em comando serial), o roteamento das
linhas que vem da serial, o parser SSE e a renovacao de sessao. O servidor e a serial sao trocados por dublês simples.

Uso (na pasta WokwiBridge):   python -m pytest

tests/fake_firmware.py continua sendo o teste manual de ponta a ponta (exige servidor no ar); o pytest nao o coleta.
"""
import json
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone

import pytest
import requests

import bridge

TIPO_SOLICITADA = "queda.confirmacao_solicitada"
TIPO_ENCERRADA = "queda.confirmacao_encerrada"
USER_ID = 1


# ------------------------------------------------------------------ apoio
def _iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def pedido(**campos) -> dict:
    """Mensagem 'queda.confirmacao_solicitada' valida; `campos` sobrescreve (ou, com valor ausente, use _sem)."""
    agora = datetime.now(timezone.utc)
    msg = {
        "schemaVersion": 3,
        "messageId": str(uuid.uuid4()),
        "messageType": TIPO_SOLICITADA,
        "userId": USER_ID,
        "episodeId": str(uuid.uuid4()),
        "issuedAt": _iso_z(agora),
        "expiresAt": _iso_z(agora + timedelta(seconds=10)),
        "payload": {"timeoutMs": 5000, "motivo": "IMPACTO_SEGUIDO_DE_IMOBILIDADE"},
    }
    msg.update(campos)
    return msg


def encerramento(resultado="CONFIRMADO", **campos) -> dict:
    msg = pedido(messageType=TIPO_ENCERRADA, payload={"resultado": resultado})
    msg.update(campos)
    return msg


def _sem(msg: dict, chave: str) -> dict:
    return {k: v for k, v in msg.items() if k != chave}


class HolderFalso:
    """Troca o SerialHolder: guarda o que o bridge tentou escrever na serial."""

    def __init__(self):
        self.linhas: list[str] = []

    def escrever(self, linha: str) -> bool:
        self.linhas.append(linha)
        return True


@pytest.fixture(autouse=True)
def ambiente(monkeypatch):
    """Configuracao do bridge fixada nos testes (o modulo le o ambiente ao ser importado)."""
    monkeypatch.setattr(bridge, "SERVIDOR", "http://servidor.teste")
    monkeypatch.setattr(bridge, "BRIDGE_ID", "bridge-teste")
    monkeypatch.setattr(bridge, "BRIDGE_SECRET", "segredo-teste")
    monkeypatch.setattr(bridge, "BRIDGE_DEVICE", "esp32-teste")


@pytest.fixture
def sessao():
    s = bridge.Sessao()
    s.access, s.refresh_token, s.user_id = "A1", "R1", USER_ID
    return s


def entregar(sessao, msg, vistas=None):
    holder = HolderFalso()
    bridge.tratar_push(sessao, holder, vistas if vistas is not None else deque(maxlen=100), msg)
    return holder.linhas


# ------------------------------------------------------------------ traduzir
def test_traduzir_solicitada_gera_buzzer_on():
    msg = pedido()
    assert bridge.traduzir(msg) == f"CMD;BUZZER_ON;{msg['messageId']};5000"


@pytest.mark.parametrize("resultado", ["CONFIRMADO", "EXPIRADO", "CANCELADO"])
def test_traduzir_encerrada_gera_buzzer_off(resultado):
    msg = encerramento(resultado)
    assert bridge.traduzir(msg) == f"CMD;BUZZER_OFF;{msg['messageId']};{resultado}"


def test_traduzir_tipo_desconhecido_devolve_none():
    assert bridge.traduzir(pedido(messageType="outro.tipo", payload={})) is None


# ------------------------------------------------------------------ tratar_push: o que chega a serial
def test_push_valido_escreve_um_comando(sessao):
    msg = pedido()
    assert entregar(sessao, msg) == [f"CMD;BUZZER_ON;{msg['messageId']};5000"]


def test_encerramento_valido_escreve_buzzer_off(sessao):
    msg = encerramento("EXPIRADO")
    assert entregar(sessao, msg) == [f"CMD;BUZZER_OFF;{msg['messageId']};EXPIRADO"]


def test_user_id_diferente_nao_escreve(sessao):
    assert entregar(sessao, pedido(userId=USER_ID + 1)) == []


def test_message_id_repetido_escreve_uma_vez(sessao):
    msg, vistas, holder = pedido(), deque(maxlen=100), HolderFalso()
    bridge.tratar_push(sessao, holder, vistas, msg)
    bridge.tratar_push(sessao, holder, vistas, msg)
    assert len(holder.linhas) == 1


def test_mensagem_expirada_nao_escreve(sessao):
    passado = _iso_z(datetime.now(timezone.utc) - timedelta(seconds=1))
    assert entregar(sessao, pedido(expiresAt=passado)) == []


def test_tipo_nao_suportado_nao_escreve(sessao):
    assert entregar(sessao, pedido(messageType="outro.tipo", payload={})) == []


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
def test_message_id_invalido_nao_escreve(sessao, message_id):
    assert entregar(sessao, pedido(messageId=message_id)) == []


def test_message_id_ausente_nao_escreve(sessao):
    assert entregar(sessao, _sem(pedido(), "messageId")) == []


@pytest.mark.parametrize("message_id", [str(uuid.uuid4()), "a", "a" * 39, "msg-123"])
def test_message_id_valido_passa(sessao, message_id):
    assert entregar(sessao, pedido(messageId=message_id)) == [f"CMD;BUZZER_ON;{message_id};5000"]


@pytest.mark.parametrize("timeout_ms", ["5000", True, False, 5000.5, 5000.0, None, [5000]])
def test_timeout_ms_que_nao_e_inteiro_nao_escreve(sessao, timeout_ms):
    assert entregar(sessao, pedido(payload={"timeoutMs": timeout_ms})) == []


def test_timeout_ms_ausente_nao_escreve(sessao):
    assert entregar(sessao, pedido(payload={"motivo": "x"})) == []


@pytest.mark.parametrize("timeout_ms", [0, -1, 999_999])
def test_faixa_do_timeout_ms_nao_e_filtrada(sessao, timeout_ms):
    """Quem decide a faixa e o firmware (push.ack REJEITADO); o bridge so repassa um inteiro."""
    msg = pedido(payload={"timeoutMs": timeout_ms})
    assert entregar(sessao, msg) == [f"CMD;BUZZER_ON;{msg['messageId']};{timeout_ms}"]


@pytest.mark.parametrize("payload", [None, [], "texto", 5])
def test_payload_que_nao_e_objeto_nao_escreve(sessao, payload):
    assert entregar(sessao, pedido(payload=payload)) == []
    assert entregar(sessao, encerramento(payload=payload)) == []


def test_payload_ausente_nao_escreve(sessao):
    assert entregar(sessao, _sem(pedido(), "payload")) == []


@pytest.mark.parametrize("resultado", ["QUALQUER", "confirmado", "CONFIRMADO;X", "CONFIRMADO\n", "", None, 1, ["CONFIRMADO"], {}])
def test_resultado_invalido_nao_escreve(sessao, resultado):
    assert entregar(sessao, pedido(messageType=TIPO_ENCERRADA, payload={"resultado": resultado})) == []


def test_resultado_ausente_nao_escreve(sessao):
    assert entregar(sessao, pedido(messageType=TIPO_ENCERRADA, payload={})) == []


@pytest.mark.parametrize("tipo", [None, 5, ["x"]])
def test_message_type_que_nao_e_texto_nao_escreve(sessao, tipo):
    assert entregar(sessao, pedido(messageType=tipo)) == []


def test_mensagem_invalida_nao_ocupa_a_lista_de_vistas(sessao):
    vistas = deque(maxlen=100)
    entregar(sessao, pedido(messageId="abc;def"), vistas)
    assert len(vistas) == 0


# ------------------------------------------------------------------ processar_linha_serial: roteamento
@pytest.fixture
def enviados(monkeypatch):
    """Troca bridge.enviar por um registrador de (rota, evento)."""
    registro = []
    monkeypatch.setattr(bridge, "enviar", lambda sessao, rota, evento: registro.append((rota, evento)))
    return registro


@pytest.mark.parametrize("linha", [
    "MPU6050 encontrado e iniciado!",        # boas-vindas do firmware
    "[DEBUG] algo",
    "[1, 2, 3]",                              # JSON, mas nao objeto (nem comeca com '{')
    '{"eventType": "imobilidade.leitura"',    # JSON truncado
    "",
])
def test_linha_que_nao_e_evento_json_e_ignorada(sessao, enviados, linha):
    bridge.processar_linha_serial(sessao, linha)
    assert enviados == []


@pytest.mark.parametrize("evento_type, rota", [
    ("push.ack", "/eventos"),
    ("confirmacao.resposta", "/eventos"),
    ("imobilidade.leitura", "/dados"),
    ("imobilidade.decisao", "/dados"),
    ("sistema.erro", "/dados"),
])
def test_roteamento_por_event_type(sessao, enviados, evento_type, rota):
    bridge.processar_linha_serial(sessao, json.dumps({"eventType": evento_type, "deviceId": "esp32-teste"}))
    assert [r for r, _ in enviados] == [rota]


def test_bridge_acrescenta_user_id_e_source(sessao, enviados):
    bridge.processar_linha_serial(sessao, json.dumps({"eventType": "push.ack", "state": "ATIVADO"}))
    (_, evento), = enviados
    assert evento["user_id"] == USER_ID and evento["source"] == "wokwi"
    assert evento["state"] == "ATIVADO"                    # o resto do evento segue intacto


def test_bridge_sobrescreve_user_id_e_source_vindos_da_serial(sessao, enviados):
    linha = json.dumps({"eventType": "imobilidade.leitura", "user_id": 99, "source": "app"})
    bridge.processar_linha_serial(sessao, linha)
    (_, evento), = enviados
    assert evento["user_id"] == USER_ID and evento["source"] == "wokwi"


# ------------------------------------------------------------------ parser SSE
def test_ler_eventos_push_completo():
    linhas = ["id: 5", "event: push", 'data: {"nonce": "a:b", "ciphertext": "c"}', ""]
    assert list(bridge._ler_eventos(linhas)) == [
        {"id": "5", "event": "push", "data": '{"nonce": "a:b", "ciphertext": "c"}'}]


def test_ler_eventos_ignora_comentario_keepalive():
    assert list(bridge._ler_eventos([":keepalive", ""])) == []
    assert list(bridge._ler_eventos([": keepalive", "id: 1", "event: push", "data: x", ""])) == [
        {"id": "1", "event": "push", "data": "x"}]


def test_ler_eventos_reautenticar_nao_tem_data():
    assert list(bridge._ler_eventos(["event: reautenticar", ""])) == [{"event": "reautenticar"}]


def test_ler_eventos_varios_eventos_e_linhas_em_branco_repetidas():
    linhas = ["id: 1", "event: push", "data: a", "", "", "id: 2", "event: push", "data: b", ""]
    assert [e["id"] for e in bridge._ler_eventos(linhas)] == ["1", "2"]


def test_ler_eventos_evento_truncado_sem_linha_em_branco_nao_e_gerado():
    linhas = ["id: 1", "event: push", "data: a", "", "id: 2", "event: push", "data: b"]
    assert [e["id"] for e in bridge._ler_eventos(linhas)] == ["1"]


def test_do_sse_cifrado_ate_a_serial(sessao):
    """Ponta a ponta sem rede: linhas SSE -> decifra -> tratar_push -> comando na serial."""
    msg = pedido()
    linhas = ["id: 7", "event: push", "data: " + json.dumps(bridge._cifrar(msg)), "", ":keepalive", ""]
    holder, vistas = HolderFalso(), deque(maxlen=100)
    for ev in bridge._ler_eventos(linhas):
        assert ev["event"] == "push"
        bridge.tratar_push(sessao, holder, vistas, bridge._decifrar(json.loads(ev["data"])))
    assert holder.linhas == [f"CMD;BUZZER_ON;{msg['messageId']};5000"]


# ------------------------------------------------------------------ SerialHolder
def test_serial_holder_sem_conexao_devolve_false():
    assert bridge.SerialHolder().escrever("CMD;BUZZER_OFF;x;CONFIRMADO") is False


def test_serial_holder_escreve_com_quebra_de_linha():
    class SerialFalsa:
        gravado = b""

        def write(self, dados):
            self.gravado += dados

    h = bridge.SerialHolder()
    h.ser = SerialFalsa()
    assert h.escrever("CMD;BUZZER_OFF;x;CONFIRMADO") is True
    assert h.ser.gravado == b"CMD;BUZZER_OFF;x;CONFIRMADO\n"


# ------------------------------------------------------------------ Sessao: login e renovacao
class RespostaFalsa:
    def __init__(self, status=200, corpo=None, texto=""):
        self.status_code, self._corpo, self.text = status, corpo, texto

    def json(self):
        return self._corpo


def tokens(access, refresh):
    """Resposta de /auth/login ou /auth/refresh: o mesmo envelope cifrado do servidor."""
    return RespostaFalsa(200, bridge._cifrar({"access_token": access, "refresh_token": refresh, "user_ids": [USER_ID]}))


@pytest.fixture
def servidor_falso(monkeypatch):
    """Troca requests.post: `respostas` e uma fila de RespostaFalsa (ou excecao); `chamadas` guarda (rota, corpo claro)."""
    class Servidor:
        respostas: list = []
        chamadas: list = []

    def post(url, **kw):
        corpo = bridge._decifrar(kw["json"]) if "json" in kw else None
        Servidor.chamadas.append((url.removeprefix(bridge.SERVIDOR), corpo))
        item = Servidor.respostas.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    Servidor.respostas, Servidor.chamadas = [], []
    monkeypatch.setattr(bridge.requests, "post", post)
    return Servidor


def test_login_envia_credenciais_fonte_e_device(servidor_falso):
    servidor_falso.respostas = [tokens("A9", "R9")]
    s = bridge.Sessao()
    s.login()
    (rota, corpo), = servidor_falso.chamadas
    assert rota == "/auth/login"
    assert corpo == {"schemaVersion": 3, "identificador": "bridge-teste", "segredo": "segredo-teste",
                     "fonte": "wokwi", "device_id": "esp32-teste"}
    assert (s.access, s.refresh_token, s.user_id) == ("A9", "R9", USER_ID)


@pytest.mark.parametrize("status", [401, 429])
def test_login_recusado_levanta_erro_com_o_codigo(servidor_falso, status):
    servidor_falso.respostas = [RespostaFalsa(status, texto="negado")]
    with pytest.raises(RuntimeError, match=str(status)):
        bridge.Sessao().login()


def test_renovar_com_refresh_200_aplica_os_tokens(sessao, servidor_falso):
    servidor_falso.respostas = [tokens("A2", "R2")]
    sessao.renovar()
    (rota, corpo), = servidor_falso.chamadas
    assert rota == "/auth/refresh" and corpo == {"schemaVersion": 3, "refresh_token": "R1"}
    assert (sessao.access, sessao.refresh_token) == ("A2", "R2")


def test_renovar_com_refresh_401_refaz_o_login(sessao, servidor_falso):
    servidor_falso.respostas = [RespostaFalsa(401, texto="expirado"), tokens("A3", "R3")]
    sessao.renovar()
    assert [r for r, _ in servidor_falso.chamadas] == ["/auth/refresh", "/auth/login"]
    assert (sessao.access, sessao.refresh_token) == ("A3", "R3")


def test_renovar_com_falha_de_rede_no_refresh_refaz_o_login(sessao, servidor_falso):
    servidor_falso.respostas = [requests.ConnectionError("fora do ar"), tokens("A4", "R4")]
    sessao.renovar()
    assert [r for r, _ in servidor_falso.chamadas] == ["/auth/refresh", "/auth/login"]
    assert sessao.access == "A4"


def test_renovar_token_ja_trocado_por_outra_thread_nao_renova_de_novo(sessao, servidor_falso):
    sessao.access = "A2"                          # outra thread ja renovou; o refresh token R1 e de uso unico
    sessao.renovar(token_recusado="A1")
    assert servidor_falso.chamadas == []
    assert sessao.access == "A2"


def test_renovar_token_recusado_igual_ao_atual_renova(sessao, servidor_falso):
    servidor_falso.respostas = [tokens("A5", "R5")]
    sessao.renovar(token_recusado="A1")
    assert sessao.access == "A5"


def test_headers_usam_o_access_atual(sessao):
    assert sessao.headers() == {"Authorization": "Bearer A1"}
