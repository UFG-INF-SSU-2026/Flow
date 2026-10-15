"""
Testes do R-12 (const.py como fonte unica) e regressao das referencias quebradas.

Estilo do projeto (DC-19): script simples com ok(...), sem pytest.
Rodar na pasta SensorServer:   python tests/test_const.py

Cobre: T-R12-1 (nenhuma constante duplicada), T-R12-2 (ajuste em const.X vale para o modulo),
T-R04-1 (margem unica), e dois guardas que pegam a classe de erro "modulo.NOME nao existe mais"
depois de mover constantes (ex.: server.py usando mensagens.SCHEMA_VERSION).
"""
import ast
import glob
import importlib
import os
import sys
import tempfile
import time

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)               # SensorServer/
sys.path.insert(0, RAIZ)
os.chdir(RAIZ)

TMP = tempfile.mkdtemp(prefix="flow_test_const_")
os.environ["FLOW_JWT_SECRET"] = "t" * 48
os.environ["FLOW_SEM_THREADS"] = "1"
os.environ["FLOW_CONTAS_PATH"] = os.path.join(TMP, "contas.json")
os.environ["FLOW_SESSOES_PATH"] = os.path.join(TMP, "sessoes.json")

import const  # noqa: E402

const.DATA_DIR = os.path.join(TMP, "dataUsers")   # nada do teste vai para a pasta real
const.CONTAS_PATH = os.environ["FLOW_CONTAS_PATH"]
const.SESSOES_PATH = os.environ["FLOW_SESSOES_PATH"]

_falhas = 0


def ok(cond: bool, nome: str, detalhe: str = "") -> None:
    global _falhas
    print(f"{'PASS' if cond else 'FAIL'}  {nome}" + (f"  -> {detalhe}" if detalhe and not cond else ""))
    if not cond:
        _falhas += 1


def _modulos_do_servidor() -> list[str]:
    nomes = sorted(os.path.basename(f)[:-3] for f in glob.glob(os.path.join(RAIZ, "*.py")))
    return [n for n in nomes if n != "simular_queda"]   # script de cliente, executa no import


def _arvore(nome: str) -> ast.Module:
    with open(os.path.join(RAIZ, nome + ".py"), encoding="utf-8") as f:
        return ast.parse(f.read())


# ---------------------------------------------------------------------------
# T-R12-1: existe uma unica definicao de cada constante, e ela esta em const.py
# ---------------------------------------------------------------------------
nomes_const = {n.targets[0].id for n in _arvore("const").body
               if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)}
nomes_const |= {n.target.id for n in _arvore("const").body if isinstance(n, ast.AnnAssign)}
ok(len(nomes_const) > 30, "T-R12-1a const.py define as constantes", f"achou {len(nomes_const)}")

duplicadas = []
for mod in _modulos_do_servidor():
    if mod == "const":
        continue
    for n in _arvore(mod).body:                       # so nivel de modulo
        alvos = []
        if isinstance(n, ast.Assign):
            alvos = [t.id for t in n.targets if isinstance(t, ast.Name)]
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            alvos = [n.target.id]
        duplicadas += [f"{mod}.{a}" for a in alvos if a in nomes_const]
ok(not duplicadas, "T-R12-1b nenhum modulo redefine nome que existe em const.py", ", ".join(duplicadas))

obrigatorias = ["MARGEM_SERVIDOR_S", "DATA_DIR", "FONTES_VALIDAS", "SCHEMA_VERSION_ATUAL", "TOLERANCIA_RELOGIO_S"]
ok(all(c in nomes_const for c in obrigatorias), "T-R12-1c constantes-chave definidas em const.py")

importa_errado = []
for mod in _modulos_do_servidor():
    for n in ast.walk(_arvore(mod)):
        if isinstance(n, ast.ImportFrom) and n.module == "const":
            importa_errado.append(mod)
ok(not importa_errado, "T-R12-1d ninguem usa 'from const import X' (DC-18)", ", ".join(importa_errado))

# const.py e modulo folha: nao importa nada do projeto
imports_const = {a.name for n in ast.walk(_arvore("const")) if isinstance(n, ast.Import) for a in n.names}
imports_const |= {n.module for n in ast.walk(_arvore("const")) if isinstance(n, ast.ImportFrom)}
ok(imports_const <= {"os"}, "T-R12-1e const.py nao importa nada do projeto (so 'os')", str(imports_const))

# ---------------------------------------------------------------------------
# Guardas: toda referencia `modulo.NOME` e `const.NOME` aponta para algo que existe
# ---------------------------------------------------------------------------
carregados = {}
for mod in _modulos_do_servidor():
    try:
        carregados[mod] = importlib.import_module(mod)
    except Exception as e:  # noqa: BLE001
        ok(False, f"T-R12-3 importar {mod}", f"{type(e).__name__}: {e}")

quebradas = []
for mod in carregados:
    for n in ast.walk(_arvore(mod)):
        if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                and n.value.id in carregados and not hasattr(carregados[n.value.id], n.attr)):
            quebradas.append(f"{mod}.py:{n.lineno} {n.value.id}.{n.attr}")
ok(not quebradas, "T-R12-3 toda referencia modulo.NOME existe", "; ".join(quebradas))

# ---------------------------------------------------------------------------
# T-R04-1: margem unica. Um ajuste em const.MARGEM_SERVIDOR_S muda o expiresAt da mensagem E o prazo do episodio
# ---------------------------------------------------------------------------
import mensagens   # noqa: E402
import regra_queda  # noqa: E402

orig = (const.TIMEOUT_CONFIRMACAO_S, const.MARGEM_SERVIDOR_S, const.COOLDOWN_S, const.TOLERANCIA_RELOGIO_S)
try:
    const.TIMEOUT_CONFIRMACAO_S, const.MARGEM_SERVIDOR_S = 1, 1
    agora = 1_000_000.0
    pedido = regra_queda.abrir_por_imobilidade(901, agora=agora)
    ok(pedido is not None, "T-R04-1a episodio aberto")
    prazo = regra_queda._episodios[901].prazo - agora
    msg = mensagens.criar_pedido_confirmacao(901, pedido.episode_id, pedido.timeout_s, pedido.motivo)
    dur = (mensagens.parse_iso_z(msg["expiresAt"]) - mensagens.parse_iso_z(msg["issuedAt"])).total_seconds()
    ok(prazo == 2 and abs(dur - 2) < 0.01, "T-R04-1b expiresAt - issuedAt e prazo do episodio = 2 s",
       f"prazo={prazo} expiresAt-issuedAt={dur}")

    # -----------------------------------------------------------------------
    # T-R12-2: const.COOLDOWN_S = 0 -> o segundo episodio seguido e aceito (e com 60 e recusado)
    # -----------------------------------------------------------------------
    const.COOLDOWN_S = 60
    regra_queda.vigiar(agora + 100)                                    # encerra 901 como EXPIRADO
    ok(regra_queda.abrir_por_imobilidade(901, agora=agora + 101) is None,
       "T-R12-2a com COOLDOWN_S=60 o segundo episodio e recusado")
    const.COOLDOWN_S = 0
    ok(regra_queda.abrir_por_imobilidade(901, agora=agora + 101) is not None,
       "T-R12-2b com const.COOLDOWN_S=0 o segundo episodio e aceito (ajuste vale para o modulo)")

    # -----------------------------------------------------------------------
    # TOLERANCIA_RELOGIO_S: ajuste em const.X vale para a deduplicacao do app (DC-08)
    # -----------------------------------------------------------------------
    def leitura(ts):
        return {"timestamp_envio": ts, "acelerometro": {"magnitude": 9.8, "magnitude_max_janela": 9.9}}

    const.TOLERANCIA_RELOGIO_S = 60
    regra_queda.eh_duplicado_app(902, "d1", leitura(1_000_000))
    ok(regra_queda.eh_duplicado_app(902, "d1", leitura(990_000)) is True,
       "T-R12-2c regressao de 10 s com tolerancia 60 s: duplicata")
    const.TOLERANCIA_RELOGIO_S = 5
    ok(regra_queda.eh_duplicado_app(902, "d1", leitura(990_000)) is False,
       "T-R12-2d com const.TOLERANCIA_RELOGIO_S=5 a mesma regressao vira nova referencia")
finally:
    (const.TIMEOUT_CONFIRMACAO_S, const.MARGEM_SERVIDOR_S,
     const.COOLDOWN_S, const.TOLERANCIA_RELOGIO_S) = orig

# ---------------------------------------------------------------------------
# Regressao E2E: as rotas nao podem dar 500 por referencia quebrada
# ---------------------------------------------------------------------------
import json  # noqa: E402

import auth  # noqa: E402
import server  # noqa: E402
from crypto_utils import decrypt_payload, encrypt_payload  # noqa: E402

auth.criar_conta("idoso1", "idoso", [1], ["app"], "senha-idoso")
cli = server.app.test_client()


def post(rota, obj, token=None):
    nonce, ct = encrypt_payload(json.dumps(obj))
    cab = {"Authorization": f"Bearer {token}"} if token else {}
    return cli.post(rota, json={"nonce": nonce, "ciphertext": ct}, headers=cab)


def corpo(resp):
    d = resp.get_json()
    return json.loads(decrypt_payload(d["nonce"], d["ciphertext"]))


r = post("/auth/login", {"identificador": "idoso1", "segredo": "senha-idoso", "fonte": "app", "device_id": "cel1"})
ok(r.status_code == 200, "REG-1 /auth/login sem schemaVersion -> 200 (aceito, DC-07)", f"status={r.status_code}")
tokens = corpo(r) if r.status_code == 200 else {}
acesso, refresh = tokens.get("access_token"), tokens.get("refresh_token")

r = post("/auth/login", {"schemaVersion": 4, "identificador": "idoso1", "segredo": "senha-idoso", "fonte": "app"})
ok(r.status_code == 400, "REG-2 /auth/login com schemaVersion 4 -> 400", f"status={r.status_code}")

r = post("/auth/refresh", {"refresh_token": refresh or "x"})
ok(r.status_code == 200, "REG-3 /auth/refresh sem schemaVersion -> 200", f"status={r.status_code}")

app_dado = {"source": "app", "user_id": 1, "device_id": "cel1", "timestamp_envio": int(time.time() * 1000),
            "acelerometro": {"magnitude": 9.8, "magnitude_max_janela": 9.9}}
r = post("/dados", {**app_dado, "schemaVersion": 3}, acesso)
ok(r.status_code == 200 and not r.get_json().get("descartado"), "REG-4 /dados v3 valido -> 200 aceito",
   f"status={r.status_code} corpo={r.get_data(as_text=True)[:120]}")

for rotulo, versao in (("4", 4), ("'3'", "3"), ("true", True)):
    r = post("/dados", {**app_dado, "schemaVersion": versao}, acesso)
    ok(r.status_code == 200 and r.get_json().get("descartado") is True,
       f"REG-5 /dados schemaVersion {rotulo} -> 200 descartado", f"status={r.status_code}")

r = post("/dados", dict(app_dado), acesso)
ok(r.status_code == 200 and not r.get_json().get("descartado"), "REG-6 /dados sem schemaVersion -> aceito como v1",
   f"status={r.status_code}")

ev = {"schemaVersion": 3, "source": "app", "user_id": 1, "device_id": "cel1", "event_time": "2026-01-01T00:00:00Z",
      "eventType": "push.ack", "state": "ATIVADO", "messageId": "m1"}
r = post("/eventos", ev, acesso)
ok(r.status_code == 200, "REG-7 /eventos v3 -> 200", f"status={r.status_code}")
r = post("/eventos", {k: v for k, v in ev.items() if k != "schemaVersion"}, acesso)
ok(r.status_code == 200 and r.get_json().get("descartado") is True, "REG-8 /eventos sem schemaVersion -> descartado (DC-07)",
   f"status={r.status_code}")

print()
if _falhas:
    print(f"FALHOU: {_falhas} caso(s).")
    sys.exit(1)
print("Todos os casos passaram.")
