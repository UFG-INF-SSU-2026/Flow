"""Handshake com e sem certificado de cliente, conexao persistente e repasse pelo bridge real (TLS em loopback)."""
import ssl
import time
from pathlib import Path

import pytest
import requests

import bridge
from apoio import HOST, certificado_de_cliente
from ligacao import Conexao
from config import Config


def _chamar(porta, pki, metodo, rota, cliente=None, **kw):
    return requests.request(metodo, f"https://{HOST}:{porta}{rota}", verify=str(pki.ca_raiz.arquivo), cert=cliente, timeout=10,
                            **kw)


class TestSemCertificado:
    """O dispositivo ainda nao tem certificado (login e emissao) ou nao o apresentou: o bridge repassa do mesmo jeito, sem o
    cabecalho de repasse, e a borda e quem responde (aqui, a borda falsa)."""

    @pytest.mark.parametrize("rota", ["/auth/login", "/auth/certificado", "/auth/refresh", "/dados", "/eventos"])
    def test_passa_sem_cabecalho_de_repasse(self, ponte, pki, borda, rota):
        r = _chamar(ponte, pki, "POST", rota, data=b'{"nonce": "n", "ciphertext": "c"}', headers={"Authorization": "Bearer t"})
        assert r.status_code == 200
        chamada = borda.chamadas(rota)[0]
        assert chamada["cabecalhos"]["Authorization"] == "Bearer t"
        assert "X-Flow-Certificado-Cliente" not in chamada["cabecalhos"]

    def test_a_recusa_da_borda_volta_como_veio(self, ponte, pki, borda):
        borda.responder("/dados", 401, b'{"status": "erro", "codigo": "certificado_ausente"}',
                        {"Content-Type": "application/json", "WWW-Authenticate": "Bearer"})
        r = _chamar(ponte, pki, "POST", "/dados", data=b"{}")
        assert r.status_code == 401 and r.json()["codigo"] == "certificado_ausente" and r.headers["WWW-Authenticate"] == "Bearer"
        borda.responder("/dados", 403, b'{"status": "erro", "codigo": "certificado_incompativel"}')
        assert _chamar(ponte, pki, "POST", "/dados", data=b"{}").json()["codigo"] == "certificado_incompativel"


class TestComCertificado:
    @pytest.mark.parametrize("metodo,rota", [("POST", "/auth/refresh"), ("POST", "/dados"), ("POST", "/eventos")])
    def test_o_certificado_do_dispositivo_vai_no_cabecalho_e_o_do_bridge_na_conexao(self, ponte, pki, borda, cliente_valido, metodo, rota):
        import base64
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        r = _chamar(ponte, pki, metodo, rota, cliente=cliente_valido, data=b"{}")
        assert r.status_code == 200
        chamada = borda.chamadas(rota)[0]
        der = x509.load_pem_x509_certificate(Path(cliente_valido[0]).read_bytes()).public_bytes(serialization.Encoding.DER)
        assert base64.b64decode(chamada["cabecalhos"]["X-Flow-Certificado-Cliente"], validate=True) == der
        assert chamada["bridge"] == "bridge"                       # a conexao com a borda traz o certificado proprio do bridge

    def test_o_cabecalho_forjado_pelo_dispositivo_nunca_chega_a_borda(self, ponte, pki, borda, cliente_valido):
        forjado = {"X-Flow-Certificado-Cliente": "FORJADO"}
        _chamar(ponte, pki, "POST", "/dados", data=b"{}", headers=forjado)                          # sem certificado
        _chamar(ponte, pki, "POST", "/eventos", cliente=cliente_valido, data=b"{}", headers=forjado)  # com certificado
        for rota in ("/dados", "/eventos"):
            assert "FORJADO" not in borda.chamadas(rota)[0]["cabecalhos"].values()
        assert "X-Flow-Certificado-Cliente" not in borda.chamadas("/dados")[0]["cabecalhos"]

    def test_o_bridge_se_identifica_tambem_no_login_e_no_sse(self, ponte, pki, borda, cliente_valido):
        _chamar(ponte, pki, "POST", "/auth/login", data=b"{}")
        assert borda.chamadas("/auth/login")[0]["bridge"] == "bridge"

    def test_certificado_de_outra_ca_derruba_o_handshake(self, ponte, pki, borda, tmp_path):
        from apoio import criar_ca
        outra = criar_ca(tmp_path, "outra-ca")
        estranho = certificado_de_cliente(tmp_path, outra, nome="estranho")
        with pytest.raises((requests.exceptions.SSLError, requests.exceptions.ConnectionError)):
            _chamar(ponte, pki, "POST", "/dados", cliente=estranho, data=b"{}")
        assert borda.registros == []

    def test_certificado_da_ca_raiz_do_projeto_tambem_e_recusado(self, ponte, pki, borda, tmp_path):
        """So a CA de dispositivos vale para o cliente do bridge: um certificado emitido pela raiz nao serve."""
        da_raiz = certificado_de_cliente(tmp_path, pki.ca_raiz, nome="da-raiz")
        with pytest.raises((requests.exceptions.SSLError, requests.exceptions.ConnectionError)):
            _chamar(ponte, pki, "POST", "/dados", cliente=da_raiz, data=b"{}")
        assert borda.registros == []

    def test_certificado_vencido_derruba_o_handshake(self, ponte, pki, borda, tmp_path):
        vencido = certificado_de_cliente(tmp_path, pki.ca_disp, nome="vencido", dias=-1)
        with pytest.raises((requests.exceptions.SSLError, requests.exceptions.ConnectionError)):
            _chamar(ponte, pki, "POST", "/dados", cliente=vencido, data=b"{}")
        assert borda.registros == []

    def test_certificado_so_server_auth_derruba_o_handshake(self, ponte, pki, borda, tmp_path):
        so_servidor = certificado_de_cliente(tmp_path, pki.ca_disp, nome="so-servidor", usos="serverAuth")
        with pytest.raises((requests.exceptions.SSLError, requests.exceptions.ConnectionError)):
            _chamar(ponte, pki, "POST", "/dados", cliente=so_servidor, data=b"{}")
        assert borda.registros == []

    def test_certificado_sem_extended_key_usage_passa_o_handshake_e_a_borda_decide(self, ponte, pki, borda, tmp_path):
        """O OpenSSL aceita certificado sem usos declarados; quem o recusa e a borda (que confere clientAuth de novo)."""
        sem_eku = certificado_de_cliente(tmp_path, pki.ca_disp, nome="sem-eku", usos=None)
        assert _chamar(ponte, pki, "POST", "/dados", cliente=sem_eku, data=b"{}").status_code == 200
        assert "X-Flow-Certificado-Cliente" in borda.chamadas("/dados")[0]["cabecalhos"]


class TestConexaoPersistente:
    def test_varias_requisicoes_na_mesma_conexao_tls(self, ponte, pki, borda, cliente_valido):
        c = Conexao(ponte, str(pki.ca_raiz.arquivo), cliente_valido)
        try:
            for rota in ("/dados", "/eventos", "/dados"):
                status, cabs, _ = c.pedir("POST", rota, b"{}")
                assert status == 200 and cabs.get("connection", "").lower() != "close"
            assert c.segue_aberta()
        finally:
            c.fechar()

    def test_corpo_acima_do_limite_da_413_sem_tocar_a_borda_e_o_bridge_segue_atendendo(self, ponte, pki, borda, cliente_valido):
        """O corpo nao e lido, entao o servidor pode fechar essa conexao (o dispositivo reconecta); o que importa e que o
        bridge continua atendendo conexoes novas."""
        from api import CORPO_MAX_BYTES
        ca = str(pki.ca_raiz.arquivo)
        c = Conexao(ponte, ca, cliente_valido)
        try:
            status, _, _ = c.pedir("POST", "/dados", b"x" * (CORPO_MAX_BYTES + 1))
            assert status == 413
        finally:
            c.fechar()
        assert borda.chamadas("/dados") == []
        outra = Conexao(ponte, ca, cliente_valido)
        try:
            assert outra.pedir("POST", "/dados", b"{}")[0] == 200
        finally:
            outra.fechar()

    def test_conexoes_ociosas_nao_ocupam_thread(self, ponte, pki, borda, cliente_valido):
        ca = str(pki.ca_raiz.arquivo)
        n = bridge.numero_de_threads(Config(servidor="https://x", ca="c", max_simulacoes=2)) + 8
        ociosas = []
        inicio = time.monotonic()
        try:
            for _ in range(n):
                c = Conexao(ponte, ca, cliente_valido)
                ociosas.append(c)
                assert c.pedir("POST", "/dados", b"{}")[0] == 200
            assert Conexao(ponte, ca, cliente_valido).pedir("POST", "/dados", b"{}")[0] == 200
            assert ociosas[0].pedir("POST", "/dados", b"{}")[0] == 200
            assert time.monotonic() - inicio < bridge.CONEXAO_OCIOSA_S - 2
        finally:
            for c in ociosas:
                c.fechar()


class TestServidor:
    def test_contexto_aceita_certificado_opcional_so_da_ca_de_dispositivos(self, pki, cfg):
        cfg = Config(servidor="https://x", ca=str(pki.ca_raiz.arquivo), api_cert=pki.servidor_cert, api_chave=pki.servidor_chave,
                     ca_dispositivos=str(pki.ca_disp.arquivo))
        ctx = bridge.contexto_tls(cfg)
        assert ctx.verify_mode == ssl.CERT_OPTIONAL and ctx.minimum_version == ssl.TLSVersion.TLSv1_2
        assert [c["subject"][0][0][1] for c in ctx.get_ca_certs()] == ["ca-dispositivos"]

    @pytest.mark.parametrize("dispositivos,esperado", [(1, 16), (8, 20), (64, 132)])
    def test_numero_de_threads(self, dispositivos, esperado):
        assert bridge.numero_de_threads(Config(servidor="https://x", ca="c", max_simulacoes=dispositivos)) == esperado

    def test_adaptador_so_tls_derruba_texto_claro(self, pki):
        import socket
        from cheroot import errors as erros_cheroot
        adaptador = bridge.AdaptadorSoTls(pki.servidor_cert, pki.servidor_chave)
        with socket.socket() as escuta:
            escuta.bind((HOST, 0))
            escuta.listen(1)
            with socket.create_connection(escuta.getsockname(), timeout=5) as cliente:
                cliente.sendall(b"POST /dados HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}")
                aceita, _ = escuta.accept()
                aceita.settimeout(5)
                try:
                    with pytest.raises(erros_cheroot.FatalSSLAlert):
                        adaptador.wrap(aceita)
                finally:
                    aceita.close()
