"""Push (SSE) repassado pelo bridge real: fluxo continuo, cabecalhos, fim do stream e isolamento entre dispositivos."""
import queue
import threading
import time

import pytest
import requests

from apoio import HOST, certificado_de_cliente
from ligacao import Conexao


def _fechar(resp):
    """requests espera a leitura em curso da thread leitora para fechar; fecha em segundo plano para nao travar o teste."""
    threading.Thread(target=resp.close, daemon=True).start()


def _abrir(porta, pki, cliente, token="t1", extras=None):
    cab = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream", **(extras or {})}
    return requests.get(f"https://{HOST}:{porta}/push/wokwi", headers=cab, stream=True, verify=str(pki.ca_raiz.arquivo),
                        cert=cliente, timeout=(5, 10))


class Leitor:
    """Uma thread por resposta le as linhas nao vazias para uma fila (varias leituras seguidas nao perdem dados)."""

    def __init__(self, resp):
        self.fila: queue.Queue = queue.Queue()
        self.fim = threading.Event()
        threading.Thread(target=self._ler, args=(resp,), daemon=True).start()

    def _ler(self, resp):
        try:
            for linha in resp.iter_lines(chunk_size=1, decode_unicode=True):
                if linha:
                    self.fila.put(linha)
        except Exception:                   # noqa: BLE001  (conexao fechada)
            pass
        finally:
            self.fim.set()

    def linhas(self, n, prazo=5.0):
        achadas, fim = [], time.monotonic() + prazo
        while len(achadas) < n and time.monotonic() < fim:
            try:
                achadas.append(self.fila.get(timeout=max(fim - time.monotonic(), 0.01)))
            except queue.Empty:
                break
        return achadas


_leitores: dict[int, Leitor] = {}


def _linhas(resp, n, prazo=5.0):
    if id(resp) not in _leitores:
        _leitores[id(resp)] = Leitor(resp)
    return _leitores[id(resp)].linhas(n, prazo)


def test_eventos_chegam_na_hora_e_intactos(ponte, pki, borda, cliente_valido):
    r = _abrir(ponte, pki, cliente_valido, extras={"Last-Event-ID": "41"})
    try:
        assert r.status_code == 200 and r.headers["Content-Type"].startswith("text/event-stream")
        assert r.headers.get("Content-Length") is None and r.headers["Cache-Control"] == "no-cache"
        assert _linhas(r, 1) == [": conectado"]
        borda.emitir(b"id: 42\nevent: push\ndata: {\"nonce\": \"n\", \"ciphertext\": \"c\"}\n\n")
        assert _linhas(r, 3) == ["id: 42", "event: push", 'data: {"nonce": "n", "ciphertext": "c"}']
        borda.emitir(b": keepalive\n\nevent: reautenticar\ndata: x\n\n")
        assert _linhas(r, 3) == [": keepalive", "event: reautenticar", "data: x"]
    finally:
        _fechar(r)
    chamada = borda.chamadas("/push/wokwi")[0]["cabecalhos"]
    assert chamada["Authorization"] == "Bearer t1" and chamada["Last-Event-Id"] == "41"      # repassados
    assert "X-Flow-Certificado-Cliente" in chamada                                           # o certificado do dispositivo, repassado
    assert borda.chamadas("/push/wokwi")[0]["bridge"] == "bridge"                            # e o bridge, identificado na conexao do SSE


def test_fim_do_stream_da_borda_encerra_o_do_dispositivo(ponte, pki, borda, cliente_valido):
    r = _abrir(ponte, pki, cliente_valido)
    try:
        assert _linhas(r, 1) == [": conectado"]
        borda.encerrar_sse()
        t0 = time.monotonic()
        _linhas(r, 0)
        assert _leitores[id(r)].fim.wait(5)                     # o corpo terminou
        assert time.monotonic() - t0 < 5
    finally:
        _fechar(r)


def test_saida_do_dispositivo_fecha_a_conexao_com_a_borda(ponte, pki, borda, cliente_valido):
    c = Conexao(ponte, str(pki.ca_raiz.arquivo), cliente_valido)
    c.sock.sendall(f"GET /push/wokwi HTTP/1.1\r\nHost: {HOST}\r\nAuthorization: Bearer t\r\n\r\n".encode())
    assert b"200" in c.sock.recv(4096)
    assert borda.sse_abertos == 1
    c.fechar()                                                  # o dispositivo sai
    for _ in range(4):                                          # o bridge so percebe a saida ao escrever (2 escritas)
        borda.emitir(b": tic\n\n")
        time.sleep(0.2)
    assert borda.sse_fechado.wait(5), "a conexao com a borda ficou aberta depois da saida do dispositivo"


def test_erro_da_borda_antes_do_stream_e_repassado(ponte, pki, borda, cliente_valido):
    borda.responder("/push/wokwi", 401, b'{"codigo": "token_invalido"}', {"Content-Type": "application/json",
                                                                          "WWW-Authenticate": "Bearer"})
    r = _abrir(ponte, pki, cliente_valido)
    assert r.status_code == 401 and r.headers["WWW-Authenticate"] == "Bearer" and r.json()["codigo"] == "token_invalido"


def test_dois_dispositivos_ao_mesmo_tempo_nao_recebem_um_do_outro(ponte, pki, borda, cliente_valido, tmp_path):
    """O isolamento e da borda (um stream por token); o bridge nao pode misturar nem reaproveitar: cada conexao leva o proprio
    Authorization e cada uma tem o proprio fluxo."""
    outro = certificado_de_cliente(tmp_path, pki.ca_disp, nome="outro")
    a = _abrir(ponte, pki, cliente_valido, token="token-A")
    b = _abrir(ponte, pki, outro, token="token-B")
    try:
        assert _linhas(a, 1) == [": conectado"] and _linhas(b, 1) == [": conectado"]
        tokens = sorted(r["cabecalhos"]["Authorization"] for r in borda.chamadas("/push/wokwi"))
        assert tokens == ["Bearer token-A", "Bearer token-B"]
        assert borda.sse_abertos == 2
    finally:
        _fechar(a)
        _fechar(b)
