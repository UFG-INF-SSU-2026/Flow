"""
Fronteiras entre os pacotes: conferidas pelo codigo-fonte, sem importar nada.

  comum/    nao importa borda nem analise (nem Flask)
  analise/  nao importa borda nem flow_db: nao tem banco; so responde a API HTTP que a borda chama
  borda/    nao importa a analise: so conversa com ela por HTTP (o contrato esta em comum/contrato_analise.py)
  flow_db/  nao importa borda nem analise

A segunda metade confere o que o teste de const nao cobre: o nucleo nao tem I/O.
"""
import ast
import glob
import os

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # SensorServer/


def _arquivos(pacote: str) -> list[str]:
    return sorted(os.path.relpath(f, RAIZ) for f in glob.glob(os.path.join(RAIZ, pacote, "*.py")))


def _raizes_importadas(arquivo: str) -> set[str]:
    """Primeiro componente de cada modulo importado (`from analise.x import y` -> analise)."""
    with open(os.path.join(RAIZ, arquivo), encoding="utf-8") as f:
        arvore = ast.parse(f.read())
    raizes = set()
    for n in ast.walk(arvore):
        if isinstance(n, ast.Import):
            raizes |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            raizes.add(n.module.split(".")[0])
    return raizes


def _proibidos(pacote: str, proibidos: set[str], excecoes: tuple[str, ...] = ()) -> list[str]:
    return [f"{arq} importa {sorted(_raizes_importadas(arq) & proibidos)}" for arq in _arquivos(pacote)
            if arq not in excecoes and _raizes_importadas(arq) & proibidos]


FERRAMENTAS_NA_RAIZ = {"simular_queda.py"}


def test_nenhum_modulo_antigo_ficou_na_raiz():
    """Depois da divisao em pacotes a raiz de SensorServer/ so tem as ferramentas. Se isto falhar, sobrou arquivo do layout antigo
    (const.py, server.py, auth.py, regra_*.py...), tipico de descompactar um zip por cima da pasta antiga: apague-os."""
    sobras = sorted(f for f in os.listdir(RAIZ) if f.endswith(".py") and f not in FERRAMENTAS_NA_RAIZ)
    assert not sobras, f"apague de SensorServer/: {', '.join(sobras)}"


def test_os_pacotes_existem():
    for pacote in ("comum", "borda", "analise", "flow_db"):
        assert _arquivos(pacote), pacote


def test_comum_nao_importa_borda_nem_analise_nem_flask():
    assert not _proibidos("comum", {"borda", "analise", "flask"})


def test_analise_nao_importa_borda_nem_banco():
    assert not _proibidos("analise", {"borda", "flow_db", "psycopg", "psycopg_pool"})


def test_so_a_api_da_analise_importa_flask():
    assert not _proibidos("analise", {"flask"}, excecoes=(os.path.join("analise", "api.py"),))


def test_flow_db_nao_importa_borda_nem_analise():
    assert not _proibidos("flow_db", {"borda", "analise"})


def test_a_borda_nao_importa_a_analise():
    assert not _proibidos("borda", {"analise"})


def test_o_nucleo_nao_faz_io_nem_usa_threads():
    """Decide sem I/O: nada de rede, banco, thread ou sistema de arquivos no nucleo."""
    assert not (_raizes_importadas(os.path.join("analise", "nucleo.py"))
                & {"flask", "requests", "psycopg", "threading", "socket", "flow_db", "os", "time"})


@pytest.mark.parametrize("arquivo", _arquivos("analise"))
def test_a_analise_nao_conhece_segredo_de_transporte_nem_senha_de_banco(arquivo):
    """Segredos por dono: chave AES, segredo JWT e senha do banco sao so da borda."""
    with open(os.path.join(RAIZ, arquivo), encoding="utf-8") as f:
        texto = f.read()
    assert "FLOW_JWT_SECRET" not in texto and "crypto_utils" not in texto and "FLOW_DB_" not in texto
