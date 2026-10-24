"""
Configuracao de conexao com o banco. So le variaveis de ambiente (o .env e carregado por comum/const.py).

Variaveis:
    FLOW_DB_HOST (127.0.0.1)  FLOW_DB_PORT (5432)  FLOW_DB_NAME (flow)
    FLOW_DB_USER (flow)       FLOW_DB_PASSWORD

Um unico usuario: o mesmo que o docker compose cria. Nenhuma senha aparece em log ou em mensagem de erro.
"""
import os

POOL_MAX = 6
TIMEOUT_CONSULTA_S = 5           # statement_timeout de cada conexao
TIMEOUT_CONEXAO_S = 3            # connect_timeout do driver
TIMEOUT_POOL_S = 2.0             # espera maxima por uma conexao livre do pool
TIMEOUT_ABERTURA_POOL_S = 5.0    # espera maxima para o pool abrir na subida


class ConfigBanco(RuntimeError):
    """Configuracao ausente ou invalida. A mensagem NUNCA contem o valor de uma senha."""


def _env(nome: str, padrao: str | None = None) -> str | None:
    valor = os.environ.get(nome)
    return valor if valor not in (None, "") else padrao


def host() -> str:
    return _env("FLOW_DB_HOST", "127.0.0.1")


def porta() -> int:
    bruto = _env("FLOW_DB_PORT", "5432")
    try:
        return int(bruto)
    except ValueError:
        raise ConfigBanco(f"FLOW_DB_PORT invalida: {bruto!r}") from None


def nome_banco() -> str:
    return _env("FLOW_DB_NAME", "flow")


def usuario() -> str:
    return _env("FLOW_DB_USER", "flow")


def dsn() -> str:
    from psycopg.conninfo import make_conninfo
    senha = _env("FLOW_DB_PASSWORD")
    if senha is None:
        raise ConfigBanco("FLOW_DB_PASSWORD nao definida. Veja .env.example.")
    return make_conninfo(host=host(), port=porta(), dbname=nome_banco(), user=usuario(), password=senha,
                         connect_timeout=TIMEOUT_CONEXAO_S, application_name="flow",
                         options=f"-c statement_timeout={TIMEOUT_CONSULTA_S * 1000}")
