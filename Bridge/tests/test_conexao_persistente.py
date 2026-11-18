"""
Conexao persistente no bridge real (loopback, TLS, certificados de teste so em pasta temporaria): varias chamadas na mesma
conexao TLS, corpo grande, conexoes ociosas que nao seguram thread, adaptador so-TLS, dimensionamento de threads e log de
acesso sem corpo nem Authorization.

Os casos de rede usam socket TLS cru (ssl), confiando so na CA de teste, para ler os cabecalhos e saber se a conexao foi
fechada; `requests.Session` esconderia isso. As rotas-sonda nao tocam o servidor: POST /v1/login com corpo `{}` (400) e
GET /v1/dispositivos/x/comandos sem token (401).
"""
import re
import socket
import ssl
import time

import pytest

import bridge
from api import CORPO_MAX_BYTES
from config import Config
from test_texto_claro_recusado import (GERADOR, HOST, _encerrar, _gerar_certificados, _porta_livre,  # noqa: F401
                                       _subir_bridge)

pytestmark = pytest.mark.skipif(
    not GERADOR.is_file(),
    reason="SensorServer/scripts/gerar_certificado.py nao encontrado ao lado de Bridge/ (gera o certificado de teste)")

PRAZO_S = 5


class Conexao:
    """Uma conexao TLS crua: manda uma requisicao e le a resposta inteira pelo Content-Length."""

    def __init__(self, porta: int, ca: str):
        ctx = ssl.create_default_context(cafile=ca)
        self.sock = ctx.wrap_socket(socket.create_connection((HOST, porta), timeout=PRAZO_S), server_hostname=HOST)
        self.sock.settimeout(PRAZO_S)

    def pedir(self, metodo: str, rota: str, corpo: bytes = b"", cabecalhos: dict | None = None) -> tuple[int, dict, bytes]:
        extra = "".join(f"{k}: {v}\r\n" for k, v in (cabecalhos or {}).items())
        cab = (f"{metodo} {rota} HTTP/1.1\r\nHost: {HOST}\r\nContent-Type: application/json\r\n{extra}"
               f"Content-Length: {len(corpo)}\r\n\r\n")
        self.sock.sendall(cab.encode() + corpo)
        return self._ler()

    def _ler(self) -> tuple[int, dict, bytes]:
        buf = b""
        while b"\r\n\r\n" not in buf:
            parte = self.sock.recv(4096)
            assert parte, "a conexao foi fechada antes de a resposta chegar"
            buf += parte
        cab, _, resto = buf.partition(b"\r\n\r\n")
        linhas = cab.decode("latin-1").split("\r\n")
        status = int(linhas[0].split()[1])
        cabecalhos = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in linhas[1:])}
        tamanho = int(cabecalhos.get("content-length", "0"))
        while len(resto) < tamanho:
            parte = self.sock.recv(4096)
            assert parte, "a conexao foi fechada no meio do corpo"
            resto += parte
        return status, cabecalhos, resto[:tamanho]

    def segue_aberta(self) -> bool:
        """True se o servidor nao fechou a conexao (nada a ler, sem EOF)."""
        self.sock.settimeout(0.3)
        try:
            return self.sock.recv(1) != b""       # b"" = EOF (fechada); algum byte seria resposta inesperada
        except (TimeoutError, ssl.SSLWantReadError):
            return True
        except OSError:
            return False
        finally:
            self.sock.settimeout(PRAZO_S)

    def fechar(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _login_vazio(c: Conexao) -> tuple[int, dict, bytes]:
    return c.pedir("POST", "/v1/login", b"{}")


def _comandos_sem_token(c: Conexao) -> tuple[int, dict, bytes]:
    return c.pedir("GET", "/v1/dispositivos/x/comandos")


@pytest.fixture
def bridge_real(tmp_path):
    ca, cert, chave = _gerar_certificados(tmp_path)
    proc, porta = _subir_bridge(tmp_path, ca, cert, chave)
    try:
        yield porta, ca, tmp_path
    finally:
        _encerrar(proc)


# C1
def test_tres_requisicoes_na_mesma_conexao_tls(bridge_real):
    porta, ca, _ = bridge_real
    c = Conexao(porta, ca)
    try:
        for chamar, esperado in ((_login_vazio, 400), (_comandos_sem_token, 401), (_login_vazio, 400)):
            status, cabecalhos, _corpo = chamar(c)
            assert status == esperado
            assert cabecalhos.get("connection", "").lower() != "close"
        assert c.segue_aberta()
    finally:
        c.fechar()


# C2
def test_corpo_acima_do_limite_da_400_e_a_conexao_segue_atendendo(bridge_real):
    porta, ca, _ = bridge_real
    c = Conexao(porta, ca)
    try:
        status, _cab, _corpo = c.pedir("POST", "/v1/login", b'{"x":"' + b"a" * (CORPO_MAX_BYTES + 1) + b'"}')
        assert status == 400
        assert _comandos_sem_token(c)[0] == 401       # mesma conexao
    finally:
        c.fechar()


# C3
def test_conexoes_ociosas_nao_ocupam_thread(bridge_real):
    porta, ca, _ = bridge_real
    ociosas = []
    inicio = time.monotonic()
    try:
        n = 30                                        # bem acima das 20 threads do padrao (8 dispositivos)
        assert n > bridge.numero_de_threads(Config(servidor="https://servidor.teste", ca="ca-teste.crt"))
        for _ in range(n):
            c = Conexao(porta, ca)
            ociosas.append(c)
            assert _comandos_sem_token(c)[0] == 401   # cada uma responde uma vez e fica ociosa

        nova = Conexao(porta, ca)                     # uma conexao nova ainda e atendida
        ociosas.append(nova)
        assert _login_vazio(nova)[0] == 400

        for c in ociosas[:5]:                         # e as ociosas ainda respondem (se o timeout estourasse, falharia aqui)
            assert _comandos_sem_token(c)[0] == 401
        assert time.monotonic() - inicio < bridge.CONEXAO_OCIOSA_S - 2, "o teste demorou perto do limite de ociosidade"
    finally:
        for c in ociosas:
            c.fechar()


# C5
@pytest.mark.parametrize("dispositivos, esperado", [(1, 16), (8, 20), (64, 132)])
def test_numero_de_threads(dispositivos, esperado):
    cfg = Config(servidor="https://servidor.teste", ca="ca-teste.crt", max_simulacoes=dispositivos)
    assert bridge.numero_de_threads(cfg) == esperado


# C6
def test_servidor_criado_usa_o_contexto_do_projeto_e_o_timeout(tmp_path):
    ca, cert, chave = _gerar_certificados(tmp_path)
    cfg = Config(servidor="https://servidor.teste", ca=ca, api_cert=cert, api_chave=chave, porta=_porta_livre())
    servidor = bridge.criar_servidor(cfg, lambda environ, start_response: [])     # nao faz bind nem inicia
    assert servidor.ssl_adapter.context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert servidor.timeout == bridge.CONEXAO_OCIOSA_S
    assert servidor.numthreads == bridge.numero_de_threads(cfg)


# C7
def test_adaptador_so_tls_derruba_texto_claro(tmp_path):
    from cheroot import errors as erros_cheroot
    ca, cert, chave = _gerar_certificados(tmp_path)
    adaptador = bridge.AdaptadorSoTls(cert, chave)
    with socket.socket() as escuta:
        escuta.bind((HOST, 0))
        escuta.listen(1)
        with socket.create_connection(escuta.getsockname(), timeout=PRAZO_S) as cliente:
            cliente.sendall(b"POST /v1/login HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}")
            aceita, _ = escuta.accept()
            aceita.settimeout(PRAZO_S)
            try:
                with pytest.raises(erros_cheroot.FatalSSLAlert):
                    adaptador.wrap(aceita)
            finally:
                aceita.close()


# C8
def test_log_de_acesso_nao_traz_corpo_nem_authorization(bridge_real):
    porta, ca, pasta = bridge_real
    c = Conexao(porta, ca)
    try:
        status, _cab, _corpo = c.pedir("POST", "/v1/login?q=QUERY-MARCADOR", b'{"campo": "CORPO-MARCADOR"}',
                                       {"Authorization": "Bearer TOKEN-MARCADOR"})
        assert status == 400
    finally:
        c.fechar()
    linha = re.compile(r"^\[bridge\] \d\d:\d\d:\d\d\.\d{3} POST /v1/login 400$", re.MULTILINE)
    fim = time.monotonic() + PRAZO_S
    texto = ""
    while time.monotonic() < fim:                     # a linha sai ao fechar a resposta, um instante depois de ela chegar
        texto = "".join(p.read_text(encoding="utf-8", errors="replace") for p in sorted(pasta.glob("bridge*.log")))
        if linha.search(texto):
            break
        time.sleep(0.1)
    assert linha.search(texto), f"linha de acesso nao encontrada:\n{texto}"
    for marcador in ("CORPO-MARCADOR", "TOKEN-MARCADOR", "QUERY-MARCADOR", "Authorization"):
        assert marcador not in texto
