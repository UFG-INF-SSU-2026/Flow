"""Pool de conexao e verificacao da versao do esquema na subida da borda."""
from . import config


class BancoIndisponivel(RuntimeError):
    pass


class BancoDesatualizado(RuntimeError):
    pass


def criar_pool():
    """Abre o pool e espera a primeira conexao. Levanta BancoIndisponivel (sem senha na mensagem)."""
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool, PoolTimeout

    pool = ConnectionPool(conninfo=config.dsn(), min_size=1, max_size=config.POOL_MAX, timeout=config.TIMEOUT_POOL_S,
                          kwargs={"row_factory": dict_row}, name="flow", open=False)
    try:
        pool.open(wait=True, timeout=config.TIMEOUT_ABERTURA_POOL_S)
    except PoolTimeout:
        pool.close()
        raise BancoIndisponivel(
            f"Nao foi possivel conectar ao banco em {config.host()}:{config.porta()}/{config.nome_banco()}. "
            f"O container esta de pe? A FLOW_DB_PASSWORD do .env confere com a do banco? O container usa o mesmo "
            f"certificado TLS (certs/)? Se o certificado foi recriado: docker compose restart db.") from None
    return pool


def versao_aplicada(conn) -> int:
    """Maior migracao aplicada (0 se a tabela de controle nao existe)."""
    existe = conn.execute("SELECT to_regclass('public.schema_migrations') IS NOT NULL AS ok").fetchone()["ok"]
    if not existe:
        return 0
    return conn.execute("SELECT COALESCE(max(versao), 0) AS v FROM schema_migrations").fetchone()["v"]


def verificar_versao_minima(pool, minima: int) -> int:
    """So LE a versao. Nao migra: a borda se recusa a subir se o banco estiver atras do que o codigo exige."""
    with pool.connection() as conn:
        atual = versao_aplicada(conn)
    if atual < minima:
        raise BancoDesatualizado(
            f"Banco desatualizado: migracao {atual} aplicada, o codigo exige >= {minima}. Rode: python -m flow_db migrar")
    return atual
