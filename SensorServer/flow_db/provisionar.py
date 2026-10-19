"""
Provisionamento dos papeis de servico (idempotente). Roda como usuario ADMINISTRATIVO.

Cria/ajusta flow_borda e flow_analise: sem superusuario, sem criar banco/papel, com limite de conexoes e tempos
limite proprios; retira do PUBLICO o direito de conectar e de criar objetos no esquema public. As permissoes por
tabela ficam na migracao 0005. Ordem de uso: docker compose up -> provisionar -> migrar.
"""
from . import config


def provisionar() -> list[str]:
    import psycopg
    from psycopg import sql

    senhas = {p: config.senha_do_papel(p) for p in config.PAPEIS}   # valida ANTES de tocar no banco
    feitos = []
    with psycopg.connect(config.dsn_admin(), autocommit=True) as conn:
        for papel in config.PAPEIS:
            existe = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (papel,)).fetchone() is not None
            verbo = "ALTER" if existe else "CREATE"
            conn.execute(sql.SQL(
                "{} ROLE {} WITH LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
                "CONNECTION LIMIT {}").format(sql.SQL(verbo), sql.Identifier(papel), sql.Literal(senhas[papel]),
                                              sql.Literal(config.limite_conexoes(papel))))
            conn.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = {}").format(
                sql.Identifier(papel), sql.Literal(f"{config.TIMEOUT_CONSULTA_S[papel]}s")))
            conn.execute(sql.SQL("ALTER ROLE {} SET idle_in_transaction_session_timeout = {}").format(
                sql.Identifier(papel), sql.Literal(f"{config.TIMEOUT_TRANSACAO_OCIOSA_S}s")))
            feitos.append(f"{papel}: {'ajustado' if existe else 'criado'}")

        banco = sql.Identifier(config.nome_banco())
        papeis = sql.SQL(", ").join(sql.Identifier(p) for p in config.PAPEIS)
        conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(banco))
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(banco, papeis))
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(papeis))
        # objetos criados pelas migracoes nao concedem nada por padrao: tudo e explicito na 0005
        conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM {}").format(papeis))
    return feitos
