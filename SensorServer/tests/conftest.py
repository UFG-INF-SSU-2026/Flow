"""
Fixtures compartilhadas (unit e integration).

Estado global do servidor (episodios, hub SSE, falhas de login...) e limpo entre testes por uma fixture autouse.
Constantes de tempo sao ajustadas com monkeypatch.setattr(const, ...) em cada teste que precisa.
"""
import json
import logging
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # SensorServer/
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

from _apoio import SEGREDO, ClienteFlask, ClienteReal  # noqa: E402

# Antes de importar o servidor: segredo e caminhos fora da pasta real. server.py cria const_comum.DATA_DIR ao importar.
_TMP = tempfile.mkdtemp(prefix="flow_tests_")
os.environ["FLOW_SEM_DOTENV"] = "1"    # os testes nunca leem o SensorServer/.env real
os.environ["FLOW_JWT_SECRET"] = SEGREDO
os.environ["FLOW_SEM_THREADS"] = "1"   # importar server nao sobe flush nem vigia
os.environ["FLOW_DB_FAKE"] = "1"       # banco falso em memoria: os testes nunca falam com PostgreSQL (so tests/banco)
os.environ["FLOW_CONTAS_PATH"] = os.path.join(_TMP, "contas.json")
os.environ["FLOW_SESSOES_PATH"] = os.path.join(_TMP, "sessoes.json")

from analise import const as const_analise  # noqa: E402
from borda import const as const_borda  # noqa: E402
from comum import const as const_comum  # noqa: E402


def pytest_collection_modifyitems(items):
    """Marca cada teste pela pasta (unit/integration), para `pytest -m unit` e `pytest -m integration`."""
    for item in items:
        partes = item.path.parts
        if "unit" in partes:
            item.add_marker("unit")
        elif "integration" in partes:
            item.add_marker("integration")

const_comum.DATA_DIR = os.path.join(_TMP, "dataUsers")

import pytest  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from analise import regra_imobilidade  # noqa: E402
from analise import regra_luz  # noqa: E402
from analise import regra_queda  # noqa: E402
from borda import auth  # noqa: E402
from borda import server  # noqa: E402
from borda import tempo  # noqa: E402
from borda.sse_hub import HUB  # noqa: E402
from comum import mensagens  # noqa: E402
from flow_db import repositorios  # noqa: E402
from _pipeline import Pipeline  # noqa: E402


def _limpar_estado() -> None:
    auth._falhas.clear()
    auth._bloqueado_ate.clear()
    for d in (regra_queda._estados, regra_queda._ultimo_log_desatualizada, regra_queda._episodios,
              regra_queda._mensagens, regra_queda._ultimo_ts_app, regra_imobilidade._estados,
              regra_luz._estados, server._buffer):
        d.clear()
    server._registros_recentes.clear()
    server._fila_leituras.clear()
    server._descartes_leituras = 0
    repositorios.usar_fake()      # banco falso novo (vazio) a cada teste
    HUB._conns.clear()
    HUB._anel.clear()
    HUB._contador.clear()


@pytest.fixture(autouse=True)
def ambiente(tmp_path, monkeypatch):
    """Isola cada teste: segredo, pastas em tmp_path, scrypt barato e estado global zerado."""
    monkeypatch.setenv("FLOW_JWT_SECRET", SEGREDO)
    monkeypatch.setattr(const_comum, "DATA_DIR", str(tmp_path / "dataUsers"))
    monkeypatch.setattr(const_borda, "CONTAS_PATH", str(tmp_path / "contas.json"))
    monkeypatch.setattr(const_borda, "SESSOES_PATH", str(tmp_path / "sessoes.json"))
    monkeypatch.setattr(const_borda, "SCRYPT_N", 2 ** 4)   # login e criar_conta em milissegundos
    os.makedirs(const_comum.DATA_DIR, exist_ok=True)
    _limpar_estado()
    yield
    _limpar_estado()


@pytest.fixture
def banco():
    """O BancoFalso do teste atual (borda e analise enxergam o mesmo). Use .borda()/.analise() para ler pelos repositorios
    ou .tabelas[...] para inspecionar."""
    return repositorios.banco_falso()


@pytest.fixture
def pipeline(request, monkeypatch):
    """A analise e a entrega de push em memoria (tests/_pipeline.py). Quem a pede enxerga, depois de cada POST em /dados e
    /eventos do `cli` (e do `servidor_real`), as decisoes ja gravadas e o push ja entregue, como no sistema de dois processos.
    O vigia do laco usa o relogio sem atraso (o relogio dos testes e congelado ou explicito)."""
    monkeypatch.setattr(const_analise, "ATRASO_VIGIA_S", 0)
    p = Pipeline()
    request.getfixturevalue("cli").apos_postar = p.drenar
    return p


@pytest.fixture
def contas():
    """idoso1 (user 1, app), idoso2 (user 2, app), bridge1 (dispositivo do user 1, wokwi) e admin1."""
    auth.criar_conta("idoso1", "idoso", [1], ["app"], "senha-idoso")
    auth.criar_conta("idoso2", "idoso", [2], ["app"], "senha-idoso2")
    auth.criar_conta("bridge1", "dispositivo", [1], ["wokwi"], "senha-bridge")
    auth.criar_conta("admin1", "admin", [], [], "senha-admin")


@pytest.fixture
def cli(contas):
    return ClienteFlask(server.app.test_client())


@pytest.fixture
def tokens(cli):
    """Access tokens por papel, mais os dois casos sem credencial valida."""
    return {
        "idoso": cli.login("idoso1", "senha-idoso", "app", "cel1")["access_token"],
        "dispositivo": cli.login("bridge1", "senha-bridge", "wokwi", "bridge-dev")["access_token"],
        "admin": cli.login("admin1", "senha-admin", None, None)["access_token"],
        "sem_token": None,
        "invalido": "abc.def.ghi",
    }


@pytest.fixture
def ler_confirmacoes():
    """Le dataUsers/confirmacoes_usuario_<id>.jsonl como lista de dicts (vazia se nao existir)."""
    def _ler(user_id: int) -> list[dict]:
        caminho = os.path.join(const_comum.DATA_DIR, f"confirmacoes_usuario_{user_id}.jsonl")
        if not os.path.exists(caminho):
            return []
        with open(caminho, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]
    return _ler


class _TempoFalso:
    """Substitui o modulo `time` dentro de auth.py (so .time() e usado la)."""

    def __init__(self, t0: float):
        self.t = t0

    def time(self) -> float:
        return self.t

    def avancar(self, segundos: float) -> None:
        self.t += segundos


@pytest.fixture
def relogio_auth(monkeypatch):
    """Relogio controlavel para auth.login/refresh. NAO afeta a validacao do JWT (PyJWT usa o relogio real)."""
    falso = _TempoFalso(1_000_000.0)
    monkeypatch.setattr(auth, "time", falso)
    return falso


class Relogio:
    """Relogio unico controlavel para o servidor, o vigia, regra_queda e mensagens (todos enxergam o mesmo instante)."""

    def __init__(self, t0: float):
        self._t = t0
        self._lock = threading.Lock()

    def agora(self) -> float:
        with self._lock:
            return self._t

    def agora_utc(self) -> datetime:
        return datetime.fromtimestamp(self.agora(), tz=timezone.utc)

    def avancar(self, segundos: float) -> None:
        with self._lock:
            self._t += segundos


@pytest.fixture
def relogio(monkeypatch):
    """Congela o tempo de tempo.agora_utc (o instante da ingestao e o do vigia, lidos pelo servidor), de
    regra_queda._agora e de mensagens.agora_utc (padroes de quem chama sem instante: entrega, replay, testes
    avulsos). issuedAt fica exato e o timeoutMs entregue e o original (sem o desconto de milissegundos do replay).
    Nao afeta o relogio do JWT nem do SSE (keepalive)."""
    r = Relogio(float(int(time.time())))
    monkeypatch.setattr(tempo, "agora_utc", r.agora_utc)
    monkeypatch.setattr(regra_queda, "_agora", r.agora)
    monkeypatch.setattr(mensagens, "agora_utc", r.agora_utc)
    return r


@pytest.fixture
def servidor_real(contas, monkeypatch, pipeline):
    """Servidor Flask real (Werkzeug, threaded) numa porta efemera, mais uma thread que roda a analise e a entrega
    (o vigia expira episodios quando o relogio de teste avanca). Devolve um ClienteReal, que tambem as roda depois de
    cada POST. Contas e constantes de tempo ja prontas; keepalive curto."""
    monkeypatch.setattr(const_borda, "KEEPALIVE_S", 0.3)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    srv = make_server("127.0.0.1", 0, server.app, threaded=True)
    parar = threading.Event()

    def ciclos():
        while not parar.is_set():
            pipeline.drenar()
            parar.wait(0.05)

    threads = [threading.Thread(target=srv.serve_forever, daemon=True), threading.Thread(target=ciclos, daemon=True)]
    for t in threads:
        t.start()
    try:
        cliente = ClienteReal(f"http://127.0.0.1:{srv.server_port}")
        cliente.apos_postar = pipeline.drenar
        yield cliente
    finally:
        parar.set()
        srv.shutdown()
        srv.server_close()
        for t in threads:
            t.join(timeout=2)
