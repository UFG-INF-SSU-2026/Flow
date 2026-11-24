"""Falhas da borda e reinicio do bridge, vistos pelo dispositivo: 502/504 rapidos, sem retentativa, bridge sem estado."""
import time

import requests

import api
from apoio import HOST
from ligacao import subir_bridge


def _post(porta, pki, rota, cliente, **kw):
    return requests.post(f"https://{HOST}:{porta}{rota}", data=b"{}", verify=str(pki.ca_raiz.arquivo), cert=cliente, timeout=15, **kw)


def test_borda_fora_da_502_rapido(pki, cliente_valido):
    import socket
    with socket.socket() as s:
        s.bind((HOST, 0))
        porta_morta = s.getsockname()[1]                           # ninguem escuta aqui
    servidor, porta, _ = subir_bridge(pki, f"https://{HOST}:{porta_morta}")
    try:
        t0 = time.monotonic()
        r = _post(porta, pki, "/dados", cliente_valido)
        assert r.status_code == 502 and r.json()["status"] == "erro"
        assert time.monotonic() - t0 < 2
    finally:
        servidor.stop()


def test_borda_lenta_da_504(ponte, pki, borda, cliente_valido, monkeypatch):
    monkeypatch.setattr(api, "TIMEOUT_S", 0.3)
    borda.responder("/dados", atraso=1.5)
    t0 = time.monotonic()
    r = _post(ponte, pki, "/dados", cliente_valido)
    assert r.status_code == 504 and time.monotonic() - t0 < 1.4
    assert len(borda.chamadas("/dados")) == 1                      # sem retentativa


def test_erro_5xx_da_borda_vira_502_sem_o_corpo_dela(ponte, pki, borda, cliente_valido):
    borda.responder("/dados", 503, b'{"codigo": "SEGREDO-DA-BORDA"}')
    r = _post(ponte, pki, "/dados", cliente_valido)
    assert r.status_code == 502 and "SEGREDO-DA-BORDA" not in r.text


def test_4xx_da_borda_volta_como_veio(ponte, pki, borda, cliente_valido):
    borda.responder("/dados", 403, b'{"codigo": "fora_de_escopo"}')
    r = _post(ponte, pki, "/dados", cliente_valido)
    assert r.status_code == 403 and r.json()["codigo"] == "fora_de_escopo"


def test_reiniciar_o_bridge_e_invisivel_ao_dispositivo(pki, borda, cliente_valido):
    """Sem estado: uma instancia nova atende a chamada seguinte com o mesmo certificado e o mesmo Authorization, sem novo login."""
    cab = {"Authorization": "Bearer mesmo-token"}
    primeiro, porta, cfg = subir_bridge(pki, borda.url)
    try:
        assert _post(porta, pki, "/dados", cliente_valido, headers=cab).status_code == 200
    finally:
        primeiro.stop()
    segundo, porta2, _ = subir_bridge(pki, borda.url)
    try:
        assert _post(porta2, pki, "/dados", cliente_valido, headers=cab).status_code == 200
    finally:
        segundo.stop()
    assert borda.chamadas("/auth/login") == []
    assert [r["cabecalhos"]["Authorization"] for r in borda.chamadas("/dados")] == ["Bearer mesmo-token"] * 2
