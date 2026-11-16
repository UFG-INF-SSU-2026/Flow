"""
Texto claro recusado: os pontos de entrada REAIS (`python -m borda`, `python -m borda.server`, `python -m analise`) sobem em
subprocesso, numa porta livre de 127.0.0.1, com o certificado de teste de `ambiente_tls()`, e so abrem TLS.

Para cada um: o HTTPS (confiando so na CA de teste) responde; uma requisicao HTTP em texto claro, mandada por socket cru para a
mesma porta, e recusada (conexao fechada, reset ou alerta TLS; qualquer resposta `HTTP/` ou estouro de prazo reprova); e depois
da recusa o HTTPS continua respondendo. Na analise a requisicao em texto claro leva o token correto, para que a recusa nao possa
ser atribuida a autenticacao.

Tudo em loopback, com banco falso e sem ler `.env` nem `certs/`. Os processos sao sempre encerrados, mesmo se o teste falhar.
"""
import contextlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

from _apoio import SEGREDO, ambiente_tls
from comum import const as const_comum

RAIZ = Path(__file__).resolve().parents[2]      # SensorServer/
TOKEN = "t" * 24
PRAZO_SUBIDA_S = 15
HOST = "127.0.0.1"

ENV_BORDA = {"FLOW_JWT_SECRET": SEGREDO, "FLOW_DB_FAKE": "1", "FLOW_ANALISE_TOKEN": TOKEN}
ENV_ANALISE = {"FLOW_ANALISE_TOKEN": TOKEN}

# (argumentos de `python`, variavel da porta, rota de saude, cabecalhos da requisicao, ambiente proprio)
PONTOS = [
    pytest.param(["-m", "borda"], "FLOW_BORDA_PORT", "/health", {}, ENV_BORDA, id="borda"),
    pytest.param(["-m", "borda.server"], "FLOW_BORDA_PORT", "/health", {}, ENV_BORDA, id="borda-desenvolvimento"),
    pytest.param(["-m", "analise"], "FLOW_ANALISE_PORT", "/saude", {const_comum.ANALISE_CABECALHO_TOKEN: TOKEN}, ENV_ANALISE,
                 id="analise"),
]


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
    pytest.fail(f"o processo nao abriu a porta {porta} em {PRAZO_SUBIDA_S} s")


def _encerrar(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@contextlib.contextmanager
def _subir(args, var_porta, env_proprio, pasta: Path):
    """Sobe o ponto de entrada numa porta livre e devolve a porta. Se a porta escolhida for tomada no intervalo entre
    escolher e abrir, repete uma vez com outra. Saida (padrao e de erro) em arquivo: nunca em PIPE sem leitura."""
    for tentativa in (1, 2):
        porta = _porta_livre()
        # sem FLOW_* herdado; sem FLASK_* tambem (FLASK_DEBUG ligaria o reloader, que cria um filho que o terminate nao alcanca)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("FLOW_", "FLASK_"))}
        env.update({"FLOW_SEM_DOTENV": "1", "PYTHONPATH": str(RAIZ), **ambiente_tls(), **env_proprio, var_porta: str(porta)})
        log = pasta / f"saida{tentativa}.log"
        with open(log, "wb") as saida:
            proc = subprocess.Popen([sys.executable, *args], cwd=pasta, env=env, stdout=saida, stderr=subprocess.STDOUT)
            try:
                if _esperar_porta(proc, porta):
                    yield porta
                    return
            finally:
                _encerrar(proc)
        texto = _fim_do_log(log)
        em_uso = any(m in texto for m in ("em uso", "already in use", "10048", "nao foi possivel abrir"))
        if tentativa == 2 or not em_uso:
            pytest.fail(f"o processo terminou antes de abrir a porta (codigo {proc.returncode}):\n{texto}")


def _requisicao_em_texto_claro(rota: str, cabecalhos: dict) -> bytes:
    linhas = [f"GET {rota} HTTP/1.1", f"Host: {HOST}", "Connection: close",
              *(f"{k}: {v}" for k, v in cabecalhos.items())]
    return ("\r\n".join(linhas) + "\r\n\r\n").encode()


def _texto_claro_e_recusado(porta: int, requisicao: bytes) -> None:
    """Manda texto claro por socket cru. Aceito como recusa: conexao fechada, reset/abortada/pipe quebrado ou alerta TLS
    (primeiro byte 0x15). Reprovado: resposta que comece com `HTTP/` ou estouro de prazo (timeout e ambiguo)."""
    primeiro = b""
    try:
        with socket.create_connection((HOST, porta), timeout=5) as s:
            s.settimeout(5)
            s.sendall(requisicao)
            primeiro = s.recv(1024)
    except TimeoutError:
        pytest.fail("a tentativa em texto claro estourou o prazo: nao da para dizer que foi recusada")
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
        return
    assert not primeiro.startswith(b"HTTP/"), f"o servidor respondeu HTTP em texto claro: {primeiro[:60]!r}"
    assert primeiro == b"" or primeiro[0] == 0x15, f"resposta inesperada ao texto claro: {primeiro[:20].hex()}"


def _https_responde(porta: int, rota: str, cabecalhos: dict, ca: str) -> requests.Response:
    return requests.get(f"https://{HOST}:{porta}{rota}", headers=cabecalhos, verify=ca, timeout=10)


@pytest.mark.parametrize("args, var_porta, rota, cabecalhos, env_proprio", PONTOS)
def test_so_abre_tls_e_recusa_texto_claro(tmp_path, args, var_porta, rota, cabecalhos, env_proprio):
    ca = ambiente_tls()["FLOW_TLS_CA"]
    with _subir(args, var_porta, env_proprio, tmp_path) as porta:
        resp = _https_responde(porta, rota, cabecalhos, ca)
        assert resp.status_code == 200
        if rota == "/saude":
            assert resp.json()["ok"] is True

        _texto_claro_e_recusado(porta, _requisicao_em_texto_claro(rota, cabecalhos))

        assert _https_responde(porta, rota, cabecalhos, ca).status_code == 200      # a recusa nao derruba o servico
