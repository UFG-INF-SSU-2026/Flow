"""O bridge fino nao guarda estado nem segredo e nao conhece o que saiu: conferido pelo codigo-fonte."""
import ast
from pathlib import Path

import pytest

PASTA = Path(__file__).resolve().parents[2]
PRODUCAO = sorted(p for p in PASTA.glob("*.py"))


def _texto(p):
    return p.read_text(encoding="utf-8")


def test_modulos_removidos_nao_voltaram():
    for nome in ("sessao.py", "traducao.py", "crypto_utils.py"):
        assert not (PASTA / nome).exists(), nome
    assert not list((PASTA / "adaptadores").glob("*.py"))


@pytest.mark.parametrize("arquivo", PRODUCAO, ids=lambda p: p.name)
def test_nenhum_modulo_importa_o_que_saiu(arquivo):
    arvore = ast.parse(_texto(arquivo))
    importados = set()
    for n in ast.walk(arvore):
        if isinstance(n, ast.Import):
            importados |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            importados.add(n.module.split(".")[0])
    assert not importados & {"sessao", "traducao", "crypto_utils", "adaptadores", "jwt", "secrets"}


def test_api_nao_tem_estado_alem_do_limitador_de_log():
    """Nenhum dicionario/lista/fila no nivel do modulo que guarde sessao, token ou segredo; so o limitador do log."""
    arvore = ast.parse(_texto(PASTA / "api.py"))
    mutaveis = []
    for n in arvore.body:
        alvo = n.target if isinstance(n, ast.AnnAssign) else (n.targets[0] if isinstance(n, ast.Assign) else None)
        valor = getattr(n, "value", None)
        if isinstance(alvo, ast.Name) and isinstance(valor, (ast.Dict, ast.List, ast.Set, ast.Call)):
            mutaveis.append(alvo.id)
    assert set(mutaveis) <= {"_ultimo_log_falha", "_lock_log", "ROTAS", "CABECALHOS_DE_IDA", "CABECALHOS_DE_VOLTA"}, mutaveis


def test_o_bridge_nunca_le_a_chave_da_ca_de_dispositivos():
    for arq in list(PRODUCAO) + [PASTA / ".env.example"]:
        for linha in _texto(arq).splitlines():
            assert "ca-dispositivos.key" not in linha.replace("#", ""), f"{arq.name}: {linha}"


def test_verificacao_do_servidor_nunca_e_desligada():
    """Nenhum `verify=False`, `CERT_NONE` ou contexto sem verificacao no codigo (docstrings e comentarios nao contam)."""
    for arq in PRODUCAO:
        for n in ast.walk(ast.parse(_texto(arq))):
            if isinstance(n, ast.keyword) and n.arg == "verify":
                assert not (isinstance(n.value, ast.Constant) and n.value.value is False), arq.name
            if isinstance(n, ast.Attribute):
                assert n.attr not in {"CERT_NONE", "_create_unverified_context"}, arq.name


def test_nenhum_literal_http_de_texto_claro_na_producao():
    for arq in PRODUCAO:
        arvore = ast.parse(_texto(arq))
        for n in ast.walk(arvore):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                assert "http://" not in n.value or len(n.value) > 60 or "https://" in n.value or arq.name in {"verificar"}, \
                    (arq.name, n.value)
