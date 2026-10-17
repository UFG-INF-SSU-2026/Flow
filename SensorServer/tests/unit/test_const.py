"""
const.py como fonte unica de configuracao.

Cobre: nenhuma constante duplicada fora de const.py (por nome e por valor), ninguem usa `from const import X`,
const.py e modulo folha, relacoes entre constantes, ajuste em `const.X` valendo para os modulos que o leem,
margem unica, e guardas contra a classe de erro "modulo.NOME nao existe mais" depois de mover constantes.
A secao final garante que as rotas nao voltam 500 por referencia quebrada.
"""
import ast
import glob
import importlib
import operator
import os
import time
from datetime import datetime, timezone

import pytest

import const
import mensagens
import regra_queda

RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # SensorServer/

_OPERADORES = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
               ast.Div: operator.truediv, ast.Pow: operator.pow}
_arvores: dict[str, ast.Module] = {}


def _modulos_do_servidor() -> list[str]:
    nomes = sorted(os.path.basename(f)[:-3] for f in glob.glob(os.path.join(RAIZ, "*.py")))
    return [n for n in nomes if n != "simular_queda"]        # script de cliente: executa ao ser importado


def _arvore(nome: str) -> ast.Module:
    if nome not in _arvores:
        with open(os.path.join(RAIZ, nome + ".py"), encoding="utf-8") as f:
            _arvores[nome] = ast.parse(f.read())
    return _arvores[nome]


def _atribuicoes(nome: str) -> list[tuple[str, ast.expr]]:
    """(nome, expressao) de cada atribuicao no nivel do modulo (so nomes simples)."""
    saida = []
    for n in _arvore(nome).body:
        if isinstance(n, ast.Assign):
            saida += [(t.id, n.value) for t in n.targets if isinstance(t, ast.Name)]
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.value is not None:
            saida.append((n.target.id, n.value))
    return saida


def _numero(no: ast.expr):
    """Valor de uma expressao numerica constante (literal, sinal e + - * / ** entre constantes), ou None."""
    if isinstance(no, ast.Constant):
        v = no.value
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    if isinstance(no, ast.UnaryOp) and isinstance(no.op, (ast.USub, ast.UAdd)):
        v = _numero(no.operand)
        return None if v is None else (-v if isinstance(no.op, ast.USub) else v)
    if isinstance(no, ast.BinOp) and type(no.op) in _OPERADORES:
        a, b = _numero(no.left), _numero(no.right)
        return None if a is None or b is None else _OPERADORES[type(no.op)](a, b)
    return None


NOMES_CONST = {nome for nome, _ in _atribuicoes("const")}
MODULOS = _modulos_do_servidor()


# ---------------------------------------------------------------------------
# Fonte unica
# ---------------------------------------------------------------------------
class TestFonteUnica:
    def test_const_define_as_constantes(self):
        assert len(NOMES_CONST) > 30

    def test_constantes_chave_existem(self):
        obrigatorias = {"MARGEM_SERVIDOR_S", "DATA_DIR", "FONTES_VALIDAS", "SCHEMA_VERSION_APP",
                        "SCHEMA_VERSION_WOKWI", "SCHEMA_VERSION_MENSAGEM", "SCHEMA_VERSION_AUTH",
                        "TOLERANCIA_RELOGIO_S", "TIMEOUT_CONFIRMACAO_S", "COOLDOWN_S", "TTL_ENCERRAMENTO_S"}
        assert obrigatorias <= NOMES_CONST

    def test_nenhum_modulo_redefine_nome_que_existe_em_const(self):
        duplicadas = [f"{mod}.{nome}" for mod in MODULOS if mod != "const"
                      for nome, _ in _atribuicoes(mod) if nome in NOMES_CONST]
        assert not duplicadas, ", ".join(duplicadas)

    def test_nenhuma_constante_numerica_de_modulo_duplica_valor_de_const(self):
        """Pega a copia com outro nome (`PRAZO = 5` quando const.TIMEOUT_CONFIRMACAO_S ja vale 5).
        Valores de modulo (abs <= 1 e 0 nao contam, por serem triviais). Se uma coincidencia for legitima,
        o valor mora no modulo por decisao documentada (ex.: crypto_utils.NONCE_SIZE_BYTES): mude o valor ou
        mova a constante para const.py."""
        em_const: dict = {}
        for nome, no in _atribuicoes("const"):
            v = _numero(no)
            if v is not None and abs(v) > 1:
                em_const.setdefault(v, []).append(nome)
        duplicadas = []
        for mod in MODULOS:
            if mod == "const":
                continue
            for nome, no in _atribuicoes(mod):
                v = _numero(no)
                if v is not None and v in em_const:
                    duplicadas.append(f"{mod}.{nome} = {v!r} (mesmo valor de const.{', const.'.join(em_const[v])})")
        assert not duplicadas, "; ".join(duplicadas)

    def test_ninguem_usa_from_const_import(self):
        importa_errado = sorted({mod for mod in MODULOS for n in ast.walk(_arvore(mod))
                                 if isinstance(n, ast.ImportFrom) and n.module == "const"})
        assert not importa_errado, ", ".join(importa_errado)

    def test_const_e_modulo_folha(self):
        arvore = _arvore("const")
        importados = {a.name for n in ast.walk(arvore) if isinstance(n, ast.Import) for a in n.names}
        importados |= {n.module for n in ast.walk(arvore) if isinstance(n, ast.ImportFrom)}
        # Folha = nao importa NENHUM modulo do projeto. pathlib e python-dotenv entram para carregar o .env na
        # importacao (as constantes de caminho leem o ambiente ao serem definidas).
        assert importados <= {"os", "pathlib", "dotenv"}, importados


class TestRelacoesEntreConstantes:
    def test_encerramento_sobrevive_ao_pedido_no_replay(self):
        assert const.TTL_ENCERRAMENTO_S > const.TIMEOUT_CONFIRMACAO_S + const.MARGEM_SERVIDOR_S

    def test_janela_de_fusao_cabe_no_intervalo_maximo_entre_leituras(self):
        assert const.JANELA_FUSAO_S <= const.INTERVALO_MAX_ENTRE_LEITURAS_S


# ---------------------------------------------------------------------------
# Guardas contra referencia quebrada
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mod", MODULOS)
def test_modulo_importa(mod):
    importlib.import_module(mod)


def test_toda_referencia_modulo_nome_existe():
    """`modulo.NOME` e `const.NOME` apontam para algo que existe (ex.: server.py usando mensagens.SCHEMA_VERSION
    depois de a constante ter ido para const.py)."""
    carregados = {mod: importlib.import_module(mod) for mod in MODULOS}
    quebradas = [f"{mod}.py:{n.lineno} {n.value.id}.{n.attr}"
                 for mod in carregados for n in ast.walk(_arvore(mod))
                 if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                 and n.value.id in carregados and not hasattr(carregados[n.value.id], n.attr)]
    assert not quebradas, "; ".join(quebradas)


# ---------------------------------------------------------------------------
# Um ajuste em const.X vale para o modulo que le const.X
# ---------------------------------------------------------------------------
def _abrir(user_id: int, agora: float):
    return regra_queda.abrir_por_imobilidade(user_id, agora=agora)


def test_margem_unica_vale_para_expiresat_e_para_o_prazo_do_episodio(monkeypatch):
    monkeypatch.setattr(const, "TIMEOUT_CONFIRMACAO_S", 1)
    monkeypatch.setattr(const, "MARGEM_SERVIDOR_S", 1)
    agora = 1_000_000.0

    pedido = _abrir(901, agora)
    assert pedido is not None and pedido.timeout_s == 1
    prazo = regra_queda._episodios[901].prazo - agora

    emissao = datetime.fromtimestamp(agora, tz=timezone.utc)
    msg = mensagens.criar_pedido_confirmacao(901, pedido.episode_id, pedido.timeout_s, pedido.motivo, agora=emissao)
    duracao = (mensagens.parse_iso_z(msg["expiresAt"]) - mensagens.parse_iso_z(msg["issuedAt"])).total_seconds()
    assert prazo == 2 and duracao == 2
    assert msg["payload"]["timeoutMs"] == 1000                  # o prazo do idoso nao inclui a margem


def test_cooldown_ajustado_em_const_vale_para_o_modulo(monkeypatch):
    monkeypatch.setattr(const, "TIMEOUT_CONFIRMACAO_S", 1)
    monkeypatch.setattr(const, "MARGEM_SERVIDOR_S", 1)
    agora = 1_000_000.0
    assert _abrir(901, agora) is not None
    regra_queda.vigiar(agora + 100)                             # encerra o episodio como EXPIRADO

    monkeypatch.setattr(const, "COOLDOWN_S", 60)
    assert _abrir(901, agora + 101) is None                     # 1 s depois do encerramento: dentro do cooldown
    monkeypatch.setattr(const, "COOLDOWN_S", 0)
    assert _abrir(901, agora + 101) is not None                 # mesmo instante, sem cooldown


def _leitura_app(ts_ms: int) -> dict:
    return {"timestamp_envio": ts_ms, "acelerometro": {"magnitude": 9.8, "magnitude_max_janela": 9.9}}


def test_tolerancia_do_relogio_ajustada_em_const_vale_para_a_deduplicacao(monkeypatch):
    monkeypatch.setattr(const, "TOLERANCIA_RELOGIO_S", 60)
    regra_queda.eh_duplicado_app(902, "d1", _leitura_app(1_000_000))
    assert regra_queda.eh_duplicado_app(902, "d1", _leitura_app(990_000)) is True     # regressao de 10 s <= 60 s

    monkeypatch.setattr(const, "TOLERANCIA_RELOGIO_S", 5)
    assert regra_queda.eh_duplicado_app(902, "d1", _leitura_app(990_000)) is False    # 10 s > 5 s: nova referencia


# ---------------------------------------------------------------------------
# Regressao: as rotas nao podem dar 500 por referencia quebrada
# ---------------------------------------------------------------------------
def _app_dado() -> dict:
    return {"source": "app", "user_id": 1, "device_id": "cel1", "timestamp_envio": int(time.time() * 1000),
            "acelerometro": {"magnitude": 9.8, "magnitude_max_janela": 9.9}}


def _evento_app() -> dict:
    return {"schemaVersion": const.SCHEMA_VERSION_APP, "source": "app", "user_id": 1, "device_id": "cel1",
            "event_time": "2026-01-01T00:00:00Z", "eventType": "push.ack", "state": "ATIVADO", "messageId": "m1"}


def _login(cli, **extra):
    corpo = {"identificador": "idoso1", "segredo": "senha-idoso", "fonte": "app", "device_id": "cel1", **extra}
    return cli.post("/auth/login", corpo)


class TestRotasSemReferenciaQuebrada:
    def test_login_sem_schemaversion_e_aceito(self, cli):
        r = _login(cli)
        assert r.status_code == 200
        assert {"access_token", "refresh_token"} <= set(cli.corpo(r))

    def test_login_com_schemaversion_maior_da_400(self, cli):
        assert _login(cli, schemaVersion=const.SCHEMA_VERSION_AUTH + 1).status_code == 400

    def test_refresh_sem_schemaversion_e_aceito(self, cli):
        sessao = cli.corpo(_login(cli))
        assert cli.post("/auth/refresh", {"refresh_token": sessao["refresh_token"]}).status_code == 200

    def test_dados_valido_e_aceito(self, cli, tokens):
        r = cli.post("/dados", {**_app_dado(), "schemaVersion": const.SCHEMA_VERSION_APP}, tokens["idoso"])
        assert r.status_code == 200 and not r.get_json().get("descartado")

    def test_dados_sem_schemaversion_e_aceito_como_versao_padrao(self, cli, tokens):
        r = cli.post("/dados", _app_dado(), tokens["idoso"])
        assert r.status_code == 200 and not r.get_json().get("descartado")

    @pytest.mark.parametrize("versao", [const.SCHEMA_VERSION_APP + 1, "3", True], ids=repr)
    def test_dados_com_versao_nao_suportada_e_descartado_com_200(self, cli, tokens, versao):
        r = cli.post("/dados", {**_app_dado(), "schemaVersion": versao}, tokens["idoso"])
        assert r.status_code == 200 and r.get_json().get("descartado") is True

    def test_eventos_valido_da_200(self, cli, tokens):
        r = cli.post("/eventos", _evento_app(), tokens["idoso"])
        assert r.status_code == 200 and r.get_json() == {"status": "ok"}

    def test_eventos_sem_schemaversion_e_aceito_como_versao_padrao(self, cli, tokens):
        corpo = {k: v for k, v in _evento_app().items() if k != "schemaVersion"}
        r = cli.post("/eventos", corpo, tokens["idoso"])
        assert r.status_code == 200 and r.get_json() == {"status": "ok"}
