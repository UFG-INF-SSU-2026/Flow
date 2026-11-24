"""Cliente-firmware -> bridge real -> borda real, tudo em TLS e com o certificado de cliente EXIGIDO pela borda.

O firmware comeca sem certificado (login e emissao), pede o seu e passa a apresenta-lo; o bridge termina o TLS do dispositivo,
repassa o certificado num cabecalho e se identifica na borda com o proprio certificado; a borda confere e compara com o token."""
import base64
import threading
from pathlib import Path

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import serialization

import api
from borda import const as const_borda
from cliente_firmware import ClienteFirmware, abrir, envelope


def _firmware(a, ident, senha, device, base=None):
    return ClienteFirmware(base or a.url_bridge, a.ca, ident, senha, device, a.pasta)


def _online(a, ident="disp1", senha="senha-1", device="esp32-1", base=None):
    f = _firmware(a, ident, senha, device, base)
    f.login()
    f.emitir()
    return f


def test_o_cabecalho_de_repasse_e_o_combinado_com_a_borda():
    assert api.CABECALHO_CERTIFICADO == const_borda.CABECALHO_CERTIFICADO_REPASSADO


def test_fluxo_completo_login_emissao_e_telemetria_pelo_bridge(ambiente):
    f = _firmware(ambiente, "disp1", "senha-1", "esp32-1")
    f.login()                                                  # 1. sem certificado
    assert f.telemetria().status_code == 401                   # 1. o resto ainda nao: quem recusa e a borda
    f.emitir()                                                 # 2. emitido e recebido (a chave nunca saiu do cliente)
    cert = x509.load_pem_x509_certificate(f.certificado.encode())
    assert cert.subject.rfc4514_string() in ("CN=esp32-1,OU=disp1", "OU=disp1,CN=esp32-1") or "esp32-1" in cert.subject.rfc4514_string()
    r = f.telemetria()                                         # 4. segue normal, com certificado
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_sem_certificado_a_borda_recusa_e_o_bridge_repassa_a_recusa(ambiente):
    f = _firmware(ambiente, "disp1", "senha-1", "esp32-1")
    f.login()
    r = f.telemetria()
    assert r.status_code == 401 and r.json()["codigo"] == "certificado_ausente"


def test_wokwi_direto_na_borda_sem_certificado_leva_401(ambiente):
    """Mudanca em relacao a versao anterior: o token `wokwi` falando direto com a borda nao passa mais sem certificado."""
    f = _firmware(ambiente, "disp1", "senha-1", "esp32-1", base=ambiente.url_borda)
    f.login()
    r = f.telemetria()
    assert r.status_code == 401 and r.json()["codigo"] == "certificado_ausente"


def test_certificado_de_outra_conta_leva_403_incompativel(ambiente):
    a, b = _online(ambiente), _online(ambiente, "disp2", "senha-2", "esp32-2")
    # o token da conta 1 com o certificado da conta 2
    a.certificado, a.chave = b.certificado, b.chave
    r = a.telemetria()
    assert r.status_code == 403 and r.json()["codigo"] == "certificado_incompativel"


def test_o_cabecalho_forjado_pelo_dispositivo_nao_vale(ambiente):
    """O ESP32 sem certificado manda o certificado (publico) de outro no cabecalho: o bridge o apaga e a borda diz que falta o certificado."""
    vitima = _online(ambiente, "disp2", "senha-2", "esp32-2")
    der = x509.load_pem_x509_certificate(vitima.certificado.encode()).public_bytes(serialization.Encoding.DER)
    f = _firmware(ambiente, "disp2", "senha-2", "esp32-2")
    f.login()
    r = f._req("POST", "/dados", {"schemaVersion": 3}, com_certificado=False,
               headers={api.CABECALHO_CERTIFICADO: base64.b64encode(der).decode()})
    assert r.status_code == 401 and r.json()["codigo"] == "certificado_ausente"


def test_o_push_exige_o_certificado_e_chega_pelo_bridge(ambiente):
    f = _online(ambiente)
    cab = {"Authorization": f"Bearer {f.access}"}
    sem = requests.get(ambiente.url_bridge + "/push/wokwi", headers=cab, verify=ambiente.ca, stream=True, timeout=5)
    assert sem.status_code == 401 and sem.json()["codigo"] == "certificado_ausente"
    sem.close()
    com = requests.get(ambiente.url_bridge + "/push/wokwi", headers=cab, verify=ambiente.ca, cert=f.arquivos_certificado(),
                       stream=True, timeout=(5, 5))
    try:
        assert com.status_code == 200 and com.headers["Content-Type"].startswith("text/event-stream")
        primeira = next(com.iter_lines(chunk_size=1))
        assert primeira.startswith(b":") or primeira.startswith(b"id") or primeira.startswith(b"event")
    finally:
        threading.Thread(target=com.close, daemon=True).start()


def test_varias_chamadas_seguidas_usam_uma_conexao_tls(ambiente):
    f = _online(ambiente)
    cert = f.arquivos_certificado()
    with requests.Session() as s:
        portas = set()
        for _ in range(4):
            r = s.post(ambiente.url_bridge + "/dados", json=envelope({"schemaVersion": 3}),
                       headers={"Authorization": f"Bearer {f.access}"}, timeout=10, verify=ambiente.ca, cert=cert)
            assert r.status_code in (200, 400)
            portas.add(r.raw.connection.sock.getsockname()[1] if r.raw.connection and r.raw.connection.sock else None)
        assert len(portas - {None}) <= 1


def test_dois_dispositivos_de_contas_diferentes_nao_se_misturam(ambiente):
    a, b = _online(ambiente), _online(ambiente, "disp2", "senha-2", "esp32-2")
    assert a.user_id == 1 and b.user_id == 2
    assert a.telemetria().status_code == 200 and b.telemetria().status_code == 200
    assert a.telemetria(user_id=2).status_code == 403              # o token da conta 1 nao vale para a pessoa 2
    assert b.telemetria(deviceId="esp32-1").status_code == 403     # nem o device de outra conta


def test_token_invalido_com_certificado_valido_da_401_de_token(ambiente):
    a = _online(ambiente)
    r = requests.post(ambiente.url_bridge + "/dados", json=envelope({"schemaVersion": 3}), verify=ambiente.ca,
                      cert=a.arquivos_certificado(), headers={"Authorization": "Bearer invalido"}, timeout=10)
    assert r.status_code == 401 and r.json()["codigo"] != "certificado_ausente"


def test_emissao_sem_a_chave_da_ca_e_503_na_borda_e_502_no_bridge_e_o_login_segue(ambiente, monkeypatch, tmp_path):
    monkeypatch.setenv("FLOW_CA_DISPOSITIVOS_KEY", str(tmp_path / "sumiu.key"))
    f = _firmware(ambiente, "disp1", "senha-1", "esp32-1")
    f.login()
    from cryptography.hazmat.primitives import hashes
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([])).sign(f.chave, hashes.SHA256())
    corpo = {"schemaVersion": 1, "csr": csr.public_bytes(serialization.Encoding.PEM).decode()}
    assert f._req("POST", "/auth/certificado", corpo, com_certificado=False).status_code == 502      # 5xx da borda vira 502 no bridge
    direto = _firmware(ambiente, "disp1", "senha-1", "esp32-1", base=ambiente.url_borda)
    direto.login()
    assert direto._req("POST", "/auth/certificado", corpo, com_certificado=False).status_code == 503
    assert f.login()["access_token"]                                                                   # o resto da borda segue
