"""
Texto claro recusado no bridge: `python bridge.py` sobe em subprocesso, numa porta livre de 127.0.0.1, com um certificado de
teste gerado na hora, e so abre TLS.

O HTTPS (confiando so na CA de teste) responde; uma requisicao HTTP em texto claro, mandada por socket cru para a mesma porta,
e recusada (conexao fechada, reset ou alerta TLS; qualquer resposta `HTTP/` ou estouro de prazo reprova); e depois da recusa o
HTTPS continua respondendo.

A sonda e a rota de login, que passa sem certificado de cliente e e recusada pelo proprio bridge (400) antes de tocar no
servidor; por isso o teste nao precisa de servidor no ar (o bridge nao o contata ao subir). Tudo em loopback, sem ler `.env` e
com certificados so em pasta temporaria. O processo e sempre encerrado, mesmo se o teste falhar.
"""
import importlib.util
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

PASTA_BRIDGE = Path(__file__).resolve().parents[1]
GERADOR = PASTA_BRIDGE.parent / "SensorServer" / "scripts" / "gerar_certificado.py"

# Rota-sonda: metodo, rota e corpo. Tem de passar sem certificado de cliente (o handshake falharia antes de qualquer resposta).
SONDA = ("POST", "/v1/login", b"{}")

HOST = "127.0.0.1"
PRAZO_SUBIDA_S = 15

pytestmark = pytest.mark.skipif(
    not GERADOR.is_file(),
    reason="SensorServer/scripts/gerar_certificado.py nao encontrado ao lado de Bridge/ (gera o certificado de teste)")


def _gerar_certificados(pasta: Path) -> tuple[str, str, str]:
    """CA de teste de poucos dias e um certificado de API para localhost e 127.0.0.1, em `pasta`. Devolve (ca, cert, chave)."""
    spec = importlib.util.spec_from_file_location("gerar_certificado", GERADOR)    # scripts/ nao e um pacote
    gerador = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gerador)
    ca, ca_chave = pasta / "ca.crt", pasta / "ca.key"
    gerador.criar_ca(ca, ca_chave, dias=2)
    cert, chave = pasta / "bridge.crt", pasta / "bridge.key"
    gerador.emitir(ca, ca_chave, cert, chave, "bridge", ["localhost"], [HOST], dias=2)
    return str(ca), str(cert), str(chave)


def _porta_livre() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def _fim_do_log(log: Path, linhas: int = 15) -> str:
    try:
        return "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-linhas:])
    except OSError:
        return "(sem saida)"


def _esperar_porta(proc: subprocess.Popen, porta: int) -> bool:
    """True se a porta aceitou conexao TCP; False se o processo terminou antes; falha se estourar o prazo."""
    fim = time.monotonic() + PRAZO_SUBIDA_S
    while time.monotonic() < fim:
        if proc.poll() is not None:
            return False
        try:
            socket.create_connection((HOST, porta), timeout=0.5).close()
            return True
        except OSError:
            time.sleep(0.1)
    pytest.fail(f"o bridge nao abriu a porta {porta} em {PRAZO_SUBIDA_S} s")


def _encerrar(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _subir_bridge(pasta: Path, ca: str, cert: str, chave: str):
    """Sobe `python bridge.py` numa porta livre. Devolve (processo, porta). Se a porta escolhida for tomada no intervalo entre
    escolher e abrir, repete uma vez com outra. Saida (padrao e de erro) em arquivo: nunca em PIPE sem leitura."""
    for tentativa in (1, 2):
        porta = _porta_livre()
        # sem FLOW_* herdado; FLOW_SERVIDOR aponta para uma porta onde nada escuta (o bridge nao contata o servidor ao subir)
        env = {k: v for k, v in os.environ.items() if not k.startswith("FLOW_")}
        env.update({"FLOW_SEM_DOTENV": "1", "FLOW_SERVIDOR": f"https://{HOST}:{_porta_livre()}", "FLOW_TLS_CA": ca,
                    "FLOW_BRIDGE_API_CERT": cert, "FLOW_BRIDGE_API_KEY": chave, "FLOW_BRIDGE_PORT": str(porta)})
        log = pasta / f"bridge{tentativa}.log"
        with open(log, "wb") as saida:
            proc = subprocess.Popen([sys.executable, str(PASTA_BRIDGE / "bridge.py")], cwd=PASTA_BRIDGE, env=env,
                                    stdout=saida, stderr=subprocess.STDOUT)
            try:
                if _esperar_porta(proc, porta):
                    return proc, porta
            except BaseException:
                _encerrar(proc)
                raise
        texto = _fim_do_log(log)
        em_uso = any(m in texto for m in ("em uso", "already in use", "10048"))
        if tentativa == 2 or not em_uso:
            pytest.fail(f"o bridge terminou antes de abrir a porta (codigo {proc.returncode}):\n{texto}")


def _requisicao_em_texto_claro() -> bytes:
    metodo, rota, corpo = SONDA
    cabecalho = (f"{metodo} {rota} HTTP/1.1\r\nHost: {HOST}\r\nContent-Type: application/json\r\n"
                 f"Content-Length: {len(corpo)}\r\nConnection: close\r\n\r\n")
    return cabecalho.encode() + corpo


def _texto_claro_e_recusado(porta: int) -> None:
    """Manda texto claro por socket cru. Aceito como recusa: conexao fechada, reset/abortada/pipe quebrado ou alerta TLS
    (primeiro byte 0x15). Reprovado: resposta que comece com `HTTP/` ou estouro de prazo (timeout e ambiguo)."""
    primeiro = b""
    try:
        with socket.create_connection((HOST, porta), timeout=5) as s:
            s.settimeout(5)
            s.sendall(_requisicao_em_texto_claro())
            primeiro = s.recv(1024)
    except TimeoutError:
        pytest.fail("a tentativa em texto claro estourou o prazo: nao da para dizer que foi recusada")
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
        return
    assert not primeiro.startswith(b"HTTP/"), f"o bridge respondeu HTTP em texto claro: {primeiro[:60]!r}"
    assert primeiro == b"" or primeiro[0] == 0x15, f"resposta inesperada ao texto claro: {primeiro[:20].hex()}"


def _sonda_https(porta: int, ca: str) -> int:
    metodo, rota, corpo = SONDA
    resp = requests.request(metodo, f"https://{HOST}:{porta}{rota}", data=corpo, headers={"Content-Type": "application/json"},
                            verify=ca, timeout=10)
    return resp.status_code


def test_bridge_real_so_abre_tls_e_recusa_texto_claro(tmp_path):
    ca, cert, chave = _gerar_certificados(tmp_path)
    proc, porta = _subir_bridge(tmp_path, ca, cert, chave)
    try:
        assert _sonda_https(porta, ca) < 500        # o handshake TLS completou e a API respondeu (hoje, 400)

        _texto_claro_e_recusado(porta)

        assert _sonda_https(porta, ca) < 500        # a recusa nao derruba a API
    finally:
        _encerrar(proc)
