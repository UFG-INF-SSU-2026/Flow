"""A subida recusa a falta de segredo e as threads de fundo sobem no import (flask run, WSGI)."""
import os
import subprocess
import sys

import pytest

from _apoio import SEGREDO

RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # SensorServer/

_SONDA = (
    "import threading, server; "
    "print(sorted(t.name for t in threading.enumerate() if t.name.startswith('flow-')))"
)


def _rodar(args, tmp_path, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("FLOW_")}
    env.update({
        "FLOW_CONTAS_PATH": str(tmp_path / "contas.json"),
        "FLOW_SESSOES_PATH": str(tmp_path / "sessoes.json"),
        "PYTHONPATH": RAIZ,
    })
    env.update(extra)
    # cwd = tmp_path: server.py cria const.DATA_DIR relativo ao diretorio atual
    return subprocess.run([sys.executable, *args], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=30)


def test_script_sem_segredo_termina_com_erro(tmp_path):
    r = _rodar([os.path.join(RAIZ, "server.py")], tmp_path)
    assert r.returncode != 0
    assert "FLOW_JWT_SECRET" in r.stderr


def test_import_sem_segredo_recusa(tmp_path):
    r = _rodar(["-c", _SONDA], tmp_path)
    assert r.returncode != 0
    assert "FLOW_JWT_SECRET" in r.stderr


def test_import_com_segredo_sobe_as_threads(tmp_path):
    r = _rodar(["-c", _SONDA], tmp_path, FLOW_JWT_SECRET=SEGREDO)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "['flow-flush', 'flow-vigia']"


def test_import_com_variavel_de_saida_nao_sobe_threads(tmp_path):
    r = _rodar(["-c", _SONDA], tmp_path, FLOW_SEM_THREADS="1")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "[]"


def test_flask_cli_sem_segredo_recusa(tmp_path):
    pytest.importorskip("flask")
    r = _rodar(["-m", "flask", "--app", "server", "routes"], tmp_path)
    assert r.returncode != 0
    assert "FLOW_JWT_SECRET" in r.stderr
