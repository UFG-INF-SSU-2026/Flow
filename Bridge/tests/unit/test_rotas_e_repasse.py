"""Lista de rotas, repasse, cabecalhos, erros e log da API (borda trocada por um dublê; o certificado entra pelo ambiente WSGI)."""
import pytest
import requests

import api
from apoio import certificado_de_cliente, criar_ca


class RespostaFalsa:
    def __init__(self, status=200, corpo=b'{"status": "ok"}', cabecalhos=None):
        self.status_code, self.content = status, corpo
        self.headers = cabecalhos or {"Content-Type": "application/json"}
        self.fechada = False

    def close(self):
        self.fechada = True

    def iter_content(self, chunk_size=None):
        yield self.content


class Borda:
    def __init__(self):
        self.chamadas = []
        self.resposta = RespostaFalsa()
        self.erro = None

    def __call__(self, metodo, url, **kw):
        self.chamadas.append({"metodo": metodo, "url": url, **kw})
        if self.erro:
            raise self.erro
        return self.resposta


@pytest.fixture
def borda():
    return Borda()


@pytest.fixture
def cert_pem(tmp_path):
    from pathlib import Path
    crt, _ = certificado_de_cliente(tmp_path, criar_ca(tmp_path, "ca"))
    return Path(crt).read_text()


@pytest.fixture
def cli(cfg, borda, cert_pem):
    app = api.criar_app(cfg, chamar=borda)
    app.testing = True
    c = app.test_client()
    c.certificado = {"SSL_CLIENT_VERIFY": "SUCCESS", "SSL_CLIENT_CERT": cert_pem}
    return c


def _pedir(cli, metodo, rota, com_certificado=True, **kw):
    return cli.open(rota, method=metodo, environ_overrides=cli.certificado if com_certificado else {}, **kw)


class TestLista:
    @pytest.mark.parametrize("metodo,rota", [("POST", "/auth/login"), ("POST", "/auth/certificado"), ("POST", "/auth/refresh"),
                                             ("POST", "/dados"), ("POST", "/eventos"), ("GET", "/push/wokwi")])
    def test_rotas_permitidas_chegam_a_borda_com_a_mesma_url(self, cli, borda, cfg, metodo, rota):
        r = _pedir(cli, metodo, rota, data=b"{}")
        assert r.status_code == 200
        assert borda.chamadas[0]["url"] == cfg.servidor + rota and borda.chamadas[0]["metodo"] == metodo

    @pytest.mark.parametrize("metodo,rota", [("GET", "/dados"), ("GET", "/health"), ("GET", "/painel"), ("GET", "/estado/x"),
                                             ("GET", "/push/app"), ("GET", "/"), ("POST", "/"), ("DELETE", "/dados"),
                                             ("GET", "/auth/login"), ("POST", "/push/wokwi"), ("POST", "/dados/"),
                                             ("POST", "/DADOS"), ("POST", "/v1/login"),
                                             ("POST", "/dados/../eventos")])
    def test_o_resto_e_404_sem_tocar_a_borda(self, cli, borda, metodo, rota):
        r = _pedir(cli, metodo, rota, data=b"{}")
        assert r.status_code == 404 and r.get_json()["status"] == "erro"
        assert borda.chamadas == []


class TestRepasse:
    def test_corpo_vai_como_bytes_sem_alteracao_e_verifica_a_ca_do_projeto(self, cli, borda, cfg):
        corpo = b'{"nonce":"a","ciphertext":"b"}\xff'
        _pedir(cli, "POST", "/dados", data=corpo)
        c = borda.chamadas[0]
        assert c["data"] == corpo and c["verify"] == cfg.ca and c["allow_redirects"] is False
        assert c["timeout"] == (api.TIMEOUT_S, api.TIMEOUT_S) and c["stream"] is False

    def test_so_a_lista_fechada_de_cabecalhos_vai(self, cli, borda):
        _pedir(cli, "POST", "/dados", com_certificado=False, data=b"{}", headers={
            "Authorization": "Bearer t", "Content-Type": "application/json", "Accept": "x", "Last-Event-ID": "3",
            "Cookie": "c=1", "X-Forwarded-For": "1.2.3.4", "X-Qualquer": "y"})
        assert borda.chamadas[0]["headers"] == {"Authorization": "Bearer t", "Content-Type": "application/json", "Accept": "x",
                                                "Last-Event-ID": "3"}

    def test_2xx_e_4xx_voltam_com_status_corpo_e_cabecalhos_permitidos(self, cli, borda):
        borda.resposta = RespostaFalsa(401, b'{"codigo": "token_invalido"}', {
            "Content-Type": "application/json", "WWW-Authenticate": "Bearer", "Set-Cookie": "a=b", "Server": "x"})
        r = _pedir(cli, "POST", "/dados", data=b"{}")
        assert r.status_code == 401 and r.get_json() == {"codigo": "token_invalido"}
        assert r.headers["WWW-Authenticate"] == "Bearer" and "Set-Cookie" not in r.headers
        assert borda.resposta.fechada

    @pytest.mark.parametrize("status", [500, 502, 503, 302])
    def test_5xx_vira_502_sem_o_corpo_da_borda(self, cli, borda, status):
        borda.resposta = RespostaFalsa(status, b"SEGREDO-INTERNO")
        r = _pedir(cli, "POST", "/dados", data=b"{}")
        assert r.status_code == 502 and b"SEGREDO-INTERNO" not in r.data and r.get_json()["status"] == "erro"

    def test_falha_de_conexao_ou_tls_vira_502_e_prazo_vira_504(self, cli, borda):
        for erro, esperado in ((requests.ConnectionError("x"), 502), (requests.exceptions.SSLError("x"), 502),
                               (requests.ReadTimeout("x"), 504), (requests.ConnectTimeout("x"), 504)):
            borda.erro = erro
            assert _pedir(cli, "POST", "/dados", data=b"{}").status_code == esperado
        assert len(borda.chamadas) == 4                          # nenhuma retentativa

    def test_corpo_acima_do_limite_da_413_sem_tocar_a_borda(self, cli, borda):
        r = _pedir(cli, "POST", "/dados", data=b"x" * (api.CORPO_MAX_BYTES + 1))
        assert r.status_code == 413 and borda.chamadas == []

    def test_corpo_no_limite_passa(self, cli, borda):
        assert _pedir(cli, "POST", "/dados", data=b"x" * api.CORPO_MAX_BYTES).status_code == 200

    def test_stream_usa_o_prazo_de_leitura_longo_e_devolve_event_stream(self, cli, borda):
        borda.resposta = RespostaFalsa(200, b": ola\n\n", {"Content-Type": "text/event-stream"})
        r = _pedir(cli, "GET", "/push/wokwi")
        assert r.status_code == 200 and r.mimetype == "text/event-stream" and r.data == b": ola\n\n"
        assert borda.chamadas[0]["stream"] is True and borda.chamadas[0]["timeout"] == (api.TIMEOUT_S, api.LEITURA_SSE_S)
        assert borda.resposta.fechada and r.headers["Cache-Control"] == "no-cache"

    def test_stream_com_erro_da_borda_e_repassado_como_erro_comum(self, cli, borda):
        borda.resposta = RespostaFalsa(403, b'{"codigo": "sem_permissao"}')
        r = _pedir(cli, "GET", "/push/wokwi")
        assert r.status_code == 403 and r.get_json()["codigo"] == "sem_permissao"


class TestCertificadoDoDispositivo:
    """O bridge nao decide por rota: com ou sem certificado, tudo chega a borda, que responde (401/403 voltam como vieram)."""

    @pytest.mark.parametrize("metodo,rota", [("POST", "/auth/login"), ("POST", "/auth/refresh"), ("POST", "/auth/certificado"),
                                             ("POST", "/dados"), ("POST", "/eventos"), ("GET", "/push/wokwi")])
    def test_sem_certificado_passa_sem_cabecalho_e_a_borda_decide(self, cli, borda, metodo, rota):
        assert _pedir(cli, metodo, rota, com_certificado=False, data=b"{}").status_code == 200
        assert api.CABECALHO_CERTIFICADO not in borda.chamadas[0]["headers"]

    def test_certificado_valido_vai_no_cabecalho_em_base64_do_der(self, cli, borda, cert_pem):
        import base64
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        _pedir(cli, "POST", "/dados", data=b"{}")
        valor = borda.chamadas[0]["headers"][api.CABECALHO_CERTIFICADO]
        assert base64.b64decode(valor, validate=True) == x509.load_pem_x509_certificate(cert_pem.encode()).public_bytes(
            serialization.Encoding.DER)

    @pytest.mark.parametrize("com_certificado", [False, True])
    def test_o_cabecalho_que_veio_do_dispositivo_e_sempre_apagado(self, cli, borda, cert_pem, com_certificado):
        """Sem isso um ESP32 sem certificado mandaria o de outro (publico) e seria aceito pela borda."""
        _pedir(cli, "POST", "/dados", com_certificado=com_certificado, data=b"{}",
               headers={api.CABECALHO_CERTIFICADO: "CERTIFICADO-DE-OUTRO", api.CABECALHO_CERTIFICADO.lower(): "x"})
        enviados = borda.chamadas[0]["headers"]
        assert "CERTIFICADO-DE-OUTRO" not in enviados.values() and "x" not in enviados.values()
        presentes = [k for k in enviados if k.lower() == api.CABECALHO_CERTIFICADO.lower()]
        assert len(presentes) == (1 if com_certificado else 0)             # so o verdadeiro, e so com certificado valido

    def test_so_se_o_handshake_validou_o_certificado(self, cli, borda, cert_pem):
        cli.certificado = {"SSL_CLIENT_VERIFY": "FAILED:x", "SSL_CLIENT_CERT": cert_pem}
        _pedir(cli, "POST", "/dados", data=b"{}")
        assert api.CABECALHO_CERTIFICADO not in borda.chamadas[0]["headers"]

    def test_o_proprio_certificado_do_bridge_vai_em_toda_chamada_inclusive_no_sse(self, cli, borda, cfg):
        for metodo, rota in (("POST", "/auth/login"), ("POST", "/dados"), ("GET", "/push/wokwi")):
            borda.chamadas.clear()
            _pedir(cli, metodo, rota, com_certificado=False, data=b"{}")
            assert borda.chamadas[0]["cert"] == (cfg.api_cert, cfg.api_chave), rota

    @pytest.mark.parametrize("status,codigo", [(401, "certificado_ausente"), (403, "certificado_incompativel")])
    def test_recusa_de_certificado_da_borda_volta_como_veio(self, cli, borda, status, codigo):
        borda.resposta = RespostaFalsa(status, ('{"codigo": "%s"}' % codigo).encode(),
                                       {"Content-Type": "application/json", "WWW-Authenticate": "Bearer"})
        r = _pedir(cli, "POST", "/dados", com_certificado=False, data=b"{}")
        assert r.status_code == status and r.get_json()["codigo"] == codigo and len(borda.chamadas) == 1       # sem retentativa


class TestLogDeFalha:
    def test_falha_repetida_escreve_no_maximo_uma_linha_por_intervalo(self, cli, borda, capsys, monkeypatch):
        api._ultimo_log_falha.clear()
        borda.erro = requests.ConnectionError("x")
        for _ in range(5):
            _pedir(cli, "POST", "/dados", data=b"{}")
        linhas = [l for l in capsys.readouterr().out.splitlines() if "borda:" in l]
        assert len(linhas) == 1 and "indisponivel" in linhas[0]
        monkeypatch.setattr(api, "INTERVALO_LOG_FALHA_S", 0)
        _pedir(cli, "POST", "/dados", data=b"{}")
        assert any("borda:" in l for l in capsys.readouterr().out.splitlines())

    def test_log_nao_traz_corpo_nem_token(self, cli, borda, capsys):
        borda.resposta = RespostaFalsa(503, b"CORPO-DA-BORDA")
        api._ultimo_log_falha.clear()
        _pedir(cli, "POST", "/dados?q=QUERY", data=b"CORPO-DO-CLIENTE", headers={"Authorization": "Bearer TOKEN-X"})
        saida = capsys.readouterr().out
        for m in ("CORPO-DA-BORDA", "CORPO-DO-CLIENTE", "TOKEN-X", "QUERY"):
            assert m not in saida
