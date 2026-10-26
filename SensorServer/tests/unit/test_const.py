"""
As tres tabelas de constantes (comum/const.py, borda/const.py, analise/const.py) como fonte unica de configuracao.

Cobre: nenhuma constante duplicada fora delas (por nome e por valor, nem entre elas), ninguem usa
`from <pacote>.const import X`, cada const so importa o permitido, relacoes entre constantes, ajuste numa constante
valendo para os modulos que a leem, margem unica, e guardas contra a classe de erro "modulo.NOME nao existe mais"
depois de mover constantes. A secao final garante que as rotas nao voltam 500 por referencia quebrada.
"""
import ast
import glob
import importlib
import operator
import os
import time
from datetime import datetime, timezone

import pytest

from analise import const as const_analise
from analise import regra_queda
from comum import const as const_comum
from comum import mensagens

RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # SensorServer/
PACOTES = ("comum", "borda", "analise")
MODULOS_CONST = {f"{p}.const" for p in PACOTES}

_OPERADORES = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
               ast.Div: operator.truediv, ast.Pow: operator.pow}
_arvores: dict[str, ast.Module] = {}


def _modulos_do_servidor() -> list[str]:
    """Modulos dos tres pacotes (nome pontuado) e os scripts da raiz que sao seguros de importar."""
    nomes = [f"{p}.{os.path.basename(f)[:-3]}" for p in PACOTES
             for f in sorted(glob.glob(os.path.join(RAIZ, p, "*.py"))) if not f.endswith("__init__.py")]
    # Na raiz so entram as ferramentas conhecidas (modulo velho esquecido na raiz e pego por test_fronteiras).
    # simular_queda fica de fora: script de cliente, executa ao importar.
    return nomes


def _arvore(nome: str) -> ast.Module:
    if nome not in _arvores:
        with open(os.path.join(RAIZ, *nome.split(".")) + ".py", encoding="utf-8") as f:
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


NOMES_POR_CONST = {m: {nome for nome, _ in _atribuicoes(m)} for m in MODULOS_CONST}
NOMES_CONST = set().union(*NOMES_POR_CONST.values())
MODULOS = _modulos_do_servidor()


# ---------------------------------------------------------------------------
# Fonte unica
# ---------------------------------------------------------------------------
class TestFonteUnica:
    def test_const_define_as_constantes(self):
        assert len(NOMES_CONST) > 30

    def test_constantes_chave_existem(self):
        obrigatorias = {"MARGEM_SERVIDOR_S", "FONTES_VALIDAS", "SCHEMA_VERSION_APP",
                        "SCHEMA_VERSION_WOKWI", "SCHEMA_VERSION_MENSAGEM", "SCHEMA_VERSION_AUTH",
                        "TOLERANCIA_RELOGIO_S", "TIMEOUT_CONFIRMACAO_S", "COOLDOWN_S", "TTL_ENCERRAMENTO_S"}
        assert obrigatorias <= NOMES_CONST

    def test_cada_constante_tem_um_unico_dono(self):
        donos: dict[str, list[str]] = {}
        for mod, nomes in NOMES_POR_CONST.items():
            for nome in nomes:
                donos.setdefault(nome, []).append(mod)
        repetidas = {n: m for n, m in donos.items() if len(m) > 1}
        assert not repetidas, repetidas

    def test_nenhum_modulo_redefine_nome_que_existe_em_const(self):
        duplicadas = [f"{mod}.{nome}" for mod in MODULOS if mod not in MODULOS_CONST
                      for nome, _ in _atribuicoes(mod) if nome in NOMES_CONST]
        assert not duplicadas, ", ".join(duplicadas)

    def test_nenhuma_constante_numerica_de_modulo_duplica_valor_de_const(self):
        """Pega a copia com outro nome (`PRAZO = 5` quando const_comum.TIMEOUT_CONFIRMACAO_S ja vale 5).
        Valores de modulo (abs <= 1 e 0 nao contam, por serem triviais). Se uma coincidencia for legitima,
        o valor mora no modulo por decisao documentada (ex.: crypto_utils.NONCE_SIZE_BYTES): mude o valor ou
        mova a constante para o const do dono."""
        em_const: dict = {}
        for mod in MODULOS_CONST:
            for nome, no in _atribuicoes(mod):
                v = _numero(no)
                if v is not None and abs(v) > 1:
                    em_const.setdefault(v, []).append(f"{mod}.{nome}")
        duplicadas = []
        for mod in MODULOS:
            if mod in MODULOS_CONST:
                continue
            for nome, no in _atribuicoes(mod):
                v = _numero(no)
                if v is not None and v in em_const:
                    duplicadas.append(f"{mod}.{nome} = {v!r} (mesmo valor de {', '.join(em_const[v])})")
        assert not duplicadas, "; ".join(duplicadas)

    def test_ninguem_usa_from_const_import(self):
        """Sempre o modulo (`from comum import const as const_comum`) e `const_comum.X` na hora do uso."""
        importa_errado = sorted({mod for mod in MODULOS for n in ast.walk(_arvore(mod))
                                 if isinstance(n, ast.ImportFrom) and n.module in MODULOS_CONST})
        assert not importa_errado, ", ".join(importa_errado)

    def test_const_comum_e_modulo_folha(self):
        arvore = _arvore("comum.const")
        importados = {a.name for n in ast.walk(arvore) if isinstance(n, ast.Import) for a in n.names}
        importados |= {n.module for n in ast.walk(arvore) if isinstance(n, ast.ImportFrom)}
        # Folha = nao importa NENHUM modulo do projeto. pathlib e python-dotenv entram para carregar o .env na
        # importacao (as constantes de caminho leem o ambiente ao serem definidas).
        assert importados <= {"os", "pathlib", "dotenv"}, importados

    @pytest.mark.parametrize("mod", ["borda.const", "analise.const"])
    def test_const_de_servico_so_importa_o_comum(self, mod):
        arvore = _arvore(mod)
        importados = {a.name for n in ast.walk(arvore) if isinstance(n, ast.Import) for a in n.names}
        importados |= {n.module for n in ast.walk(arvore) if isinstance(n, ast.ImportFrom)}
        assert importados <= {"os", "comum"}, importados


class TestRelacoesEntreConstantes:
    def test_encerramento_sobrevive_ao_pedido_no_replay(self):
        assert const_comum.TTL_ENCERRAMENTO_S > const_comum.TIMEOUT_CONFIRMACAO_S + const_comum.MARGEM_SERVIDOR_S

    def test_janela_de_fusao_cabe_no_intervalo_maximo_entre_leituras(self):
        assert const_analise.JANELA_FUSAO_S <= const_analise.INTERVALO_MAX_ENTRE_LEITURAS_S


# ---------------------------------------------------------------------------
# Guardas contra referencia quebrada
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mod", MODULOS)
def test_modulo_importa(mod):
    importlib.import_module(mod)


def _apelidos(mod: str, carregados: dict) -> dict:
    """Nome local -> modulo carregado, a partir dos imports do proprio arquivo (`from pacote import modulo [as x]`)."""
    saida = {}
    for n in ast.walk(_arvore(mod)):
        if isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            for a in n.names:
                alvo = f"{n.module}.{a.name}"
                if alvo in carregados:
                    saida[a.asname or a.name] = carregados[alvo]
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name in carregados:
                    saida[a.asname or a.name] = carregados[a.name]
    return saida


def test_toda_referencia_modulo_nome_existe():
    """`modulo.NOME` e `const_x.NOME` apontam para algo que existe (ex.: server.py usando mensagens.SCHEMA_VERSION
    depois de a constante ter ido para const)."""
    carregados = {mod: importlib.import_module(mod) for mod in MODULOS}
    quebradas = []
    for mod in carregados:
        apelidos = _apelidos(mod, carregados)
        for n in ast.walk(_arvore(mod)):
            if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in apelidos
                    and not hasattr(apelidos[n.value.id], n.attr)):
                quebradas.append(f"{mod}.py:{n.lineno} {n.value.id}.{n.attr}")
    assert not quebradas, "; ".join(quebradas)


# ---------------------------------------------------------------------------
# Um ajuste numa constante vale para o modulo que a le
# ---------------------------------------------------------------------------
def _abrir(user_id: int, agora: float):
    return regra_queda.abrir_por_imobilidade(user_id, agora=agora)


def test_margem_unica_vale_para_expiresat_e_para_o_prazo_do_episodio(monkeypatch):
    monkeypatch.setattr(const_comum, "TIMEOUT_CONFIRMACAO_S", 1)
    monkeypatch.setattr(const_comum, "MARGEM_SERVIDOR_S", 1)
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
    monkeypatch.setattr(const_comum, "TIMEOUT_CONFIRMACAO_S", 1)
    monkeypatch.setattr(const_comum, "MARGEM_SERVIDOR_S", 1)
    agora = 1_000_000.0
    assert _abrir(901, agora) is not None
    regra_queda.vigiar(agora + 100)                             # encerra o episodio como EXPIRADO

    monkeypatch.setattr(const_analise, "COOLDOWN_S", 60)
    assert _abrir(901, agora + 101) is None                     # 1 s depois do encerramento: dentro do cooldown
    monkeypatch.setattr(const_analise, "COOLDOWN_S", 0)
    assert _abrir(901, agora + 101) is not None                 # mesmo instante, sem cooldown


def _leitura_app(ts_ms: int) -> dict:
    return {"timestamp_envio": ts_ms, "acelerometro": {"magnitude": 9.8, "magnitude_max_janela": 9.9}}


def test_tolerancia_do_relogio_ajustada_em_const_vale_para_a_deduplicacao(monkeypatch):
    monkeypatch.setattr(const_analise, "TOLERANCIA_RELOGIO_S", 60)
    regra_queda.eh_duplicado_app(902, "d1", _leitura_app(1_000_000))
    assert regra_queda.eh_duplicado_app(902, "d1", _leitura_app(990_000)) is True     # regressao de 10 s <= 60 s

    monkeypatch.setattr(const_analise, "TOLERANCIA_RELOGIO_S", 5)
    assert regra_queda.eh_duplicado_app(902, "d1", _leitura_app(990_000)) is False    # 10 s > 5 s: nova referencia


# ---------------------------------------------------------------------------
# Regressao: as rotas nao podem dar 500 por referencia quebrada
# ---------------------------------------------------------------------------
def _app_dado() -> dict:
    return {"source": "app", "user_id": 1, "device_id": "cel1", "timestamp_envio": int(time.time() * 1000),
            "acelerometro": {"magnitude": 9.8, "magnitude_max_janela": 9.9}}


def _evento_app() -> dict:
    return {"schemaVersion": const_comum.SCHEMA_VERSION_APP, "source": "app", "user_id": 1, "device_id": "cel1",
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
        assert _login(cli, schemaVersion=const_comum.SCHEMA_VERSION_AUTH + 1).status_code == 400

    def test_refresh_sem_schemaversion_e_aceito(self, cli):
        sessao = cli.corpo(_login(cli))
        assert cli.post("/auth/refresh", {"refresh_token": sessao["refresh_token"]}).status_code == 200

    def test_dados_valido_e_aceito(self, cli, tokens):
        r = cli.post("/dados", {**_app_dado(), "schemaVersion": const_comum.SCHEMA_VERSION_APP}, tokens["idoso"])
        assert r.status_code == 200 and not r.get_json().get("descartado")

    def test_dados_sem_schemaversion_e_aceito_como_versao_padrao(self, cli, tokens):
        r = cli.post("/dados", _app_dado(), tokens["idoso"])
        assert r.status_code == 200 and not r.get_json().get("descartado")

    @pytest.mark.parametrize("versao", [const_comum.SCHEMA_VERSION_APP + 1, "3", True], ids=repr)
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
