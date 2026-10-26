"""
Executor de migracoes. Arquivos SQL numerados em flow_db/migrations/ (NNNN_nome.sql), aplicados em ordem, cada um numa
transacao propria junto com o registro em schema_migrations.

- Trava de aviso (pg_advisory_lock): duas execucoes simultaneas nao se atropelam; a segunda espera e nao acha nada a fazer.
- Nunca edite uma migracao ja aplicada: crie uma nova. Nao ha "desfazer".
- Roda com o papel ADMINISTRATIVO (config.dsn_admin). `garantir_papel_borda` cria (ou atualiza) o papel `flow_borda`, que so
  le e escreve nas tabelas do app (sem DDL); a senha vem de FLOW_DB_PASSWORD e nunca aparece em SQL versionado.
"""
import re
from dataclasses import dataclass
from pathlib import Path

from . import config

DIRETORIO = Path(__file__).with_name("migrations")
CHAVE_TRAVA = 0x464C4F57   # "FLOW"
_NOME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migracao:
    versao: int
    nome: str
    caminho: Path


class MigracaoInvalida(RuntimeError):
    pass


def listar(diretorio: Path = DIRETORIO) -> list[Migracao]:
    """Migracoes em ordem. Recusa nome fora do padrao, versao repetida e buraco na sequencia (comeca em 1)."""
    achadas = []
    for arq in sorted(diretorio.glob("*.sql")):
        m = _NOME.match(arq.name)
        if not m:
            raise MigracaoInvalida(f"nome de migracao fora do padrao NNNN_nome.sql: {arq.name}")
        achadas.append(Migracao(int(m.group(1)), m.group(2), arq))
    for esperado, mig in enumerate(achadas, start=1):
        if mig.versao != esperado:
            raise MigracaoInvalida(f"sequencia de migracoes quebrada: esperado {esperado:04d}, achei {mig.caminho.name}")
    return achadas


def ultima_disponivel(diretorio: Path = DIRETORIO) -> int:
    lista = listar(diretorio)
    return lista[-1].versao if lista else 0


def aplicar(conn=None, diretorio: Path = DIRETORIO) -> list[int]:
    """Aplica as pendentes. Devolve as versoes aplicadas agora (lista vazia = ja estava em dia)."""
    import psycopg
    proprio = conn is None
    if proprio:
        conn = psycopg.connect(config.dsn_admin(), autocommit=True)
    try:
        conn.execute("SELECT pg_advisory_lock(%s)", (CHAVE_TRAVA,))
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                                versao integer PRIMARY KEY, nome text NOT NULL,
                                aplicada_em timestamptz NOT NULL DEFAULT now())""")
            feitas = {l[0] for l in conn.execute("SELECT versao FROM schema_migrations").fetchall()}
            aplicadas = []
            for mig in listar(diretorio):
                if mig.versao in feitas:
                    continue
                sql = mig.caminho.read_text(encoding="utf-8")
                with conn.transaction():
                    conn.execute(sql)     # sem parametros: o driver aceita varios comandos
                    conn.execute("INSERT INTO schema_migrations (versao, nome) VALUES (%s, %s)", (mig.versao, mig.nome))
                aplicadas.append(mig.versao)
            return aplicadas
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (CHAVE_TRAVA,))
    finally:
        if proprio:
            conn.close()


def garantir_papel_borda(conn=None) -> None:
    """Cria o papel da borda (ou so atualiza a senha, se ja existe) e concede o minimo: conectar, usar o esquema `public`,
    SELECT/INSERT/UPDATE/DELETE nas tabelas (as de hoje e as futuras, `ALTER DEFAULT PRIVILEGES`) e usar as sequencias.
    Sem DDL; em `schema_migrations` so le (a subida confere a versao). Idempotente. Usa a conexao administrativa."""
    import psycopg
    from psycopg import sql
    senha = config.senha_borda()
    papel = sql.Identifier(config.usuario())
    proprio = conn is None
    if proprio:
        conn = psycopg.connect(config.dsn_admin(), autocommit=True)
    try:
        existe = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (config.usuario(),)).fetchone() is not None
        verbo = "ALTER ROLE {} WITH LOGIN PASSWORD {}" if existe else "CREATE ROLE {} LOGIN PASSWORD {}"
        conn.execute(sql.SQL(verbo).format(papel, sql.Literal(senha)))
        banco = conn.execute("SELECT current_database()").fetchone()[0]
        for comando in (
            "GRANT CONNECT ON DATABASE {banco} TO {p}",
            "GRANT USAGE ON SCHEMA public TO {p}",
            "REVOKE CREATE ON SCHEMA public FROM {p}",
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {p}",
            "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {p}",
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {p}",
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {p}",
            "REVOKE INSERT, UPDATE, DELETE ON schema_migrations FROM {p}",
        ):
            conn.execute(sql.SQL(comando).format(banco=sql.Identifier(banco), p=papel))
    finally:
        if proprio:
            conn.close()
