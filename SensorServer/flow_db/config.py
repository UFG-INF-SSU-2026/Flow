"""
Configuracao de conexao com o banco. So le variaveis de ambiente (o .env e carregado por comum/const.py).

Variaveis:
    FLOW_DB_HOST (127.0.0.1)  FLOW_DB_PORT (5432)  FLOW_DB_NAME (flow)  FLOW_DB_SSLMODE (verify-full)
    FLOW_DB_USER (flow_borda) FLOW_DB_PASSWORD      o papel da borda: SELECT/INSERT/UPDATE/DELETE, sem DDL
    FLOW_DB_ADMIN_USER (flow) FLOW_DB_ADMIN_PASSWORD  o papel administrativo (o que o docker compose cria, dono do esquema)

`dsn()` e o da borda (e da CLI de contas). `dsn_admin()` so existe para `python -m flow_db` (migracao e status) e para o
backup: o carregador do .env so poe FLOW_DB_ADMIN_* no ambiente desse comando (comum/const.py). Nenhuma senha aparece em
log ou em mensagem de erro.

A conexao e SEMPRE cifrada e o certificado do banco e conferido: sslmode `verify-full` (padrao: confere a cadeia e o nome ou
IP do host contra o certificado) ou `verify-ca` (so a cadeia). A confianca e a CA do projeto (`sslrootcert` = FLOW_TLS_CA,
comum/tls.py).
Nao ha modo sem TLS.
"""
import os

SSLMODES = ("verify-full", "verify-ca")   # os unicos aceitos: toda ponta fala TLS e confere o certificado
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


PAPEL_BORDA = "flow_borda"       # nome padrao do papel da borda (FLOW_DB_USER)


def usuario() -> str:
    return _env("FLOW_DB_USER", PAPEL_BORDA)


def usuario_admin() -> str:
    return _env("FLOW_DB_ADMIN_USER", "flow")


def sslmode() -> str:
    modo = _env("FLOW_DB_SSLMODE", "verify-full")
    if modo not in SSLMODES:
        raise ConfigBanco(f"FLOW_DB_SSLMODE invalido: {modo!r} (use {' ou '.join(SSLMODES)}; o banco so fala TLS)")
    return modo


def _dsn(usuario_: str, senha: str | None, variavel_da_senha: str) -> str:
    from psycopg.conninfo import make_conninfo
    from comum import tls
    if senha is None:
        raise ConfigBanco(f"{variavel_da_senha} nao definida. Veja .env.example.")
    try:
        raiz_de_confianca = tls.ca()
    except tls.ConfigTls as e:
        raise ConfigBanco(str(e)) from None
    return make_conninfo(host=host(), port=porta(), dbname=nome_banco(), user=usuario_, password=senha,
                         sslmode=sslmode(), sslrootcert=raiz_de_confianca,
                         connect_timeout=TIMEOUT_CONEXAO_S, application_name="flow",
                         options=f"-c statement_timeout={TIMEOUT_CONSULTA_S * 1000}")


def senha_borda() -> str:
    """Senha do papel da borda (FLOW_DB_PASSWORD). Quem a define no banco e `python -m flow_db migrar`."""
    senha = _env("FLOW_DB_PASSWORD")
    if senha is None:
        raise ConfigBanco("FLOW_DB_PASSWORD nao definida. Veja .env.example.")
    return senha


def dsn() -> str:
    """Conexao da borda (papel sem DDL)."""
    return _dsn(usuario(), _env("FLOW_DB_PASSWORD"), "FLOW_DB_PASSWORD")


def dsn_admin() -> str:
    """Conexao administrativa: migracao, status e backup. So `python -m flow_db` tem FLOW_DB_ADMIN_* no ambiente."""
    return _dsn(usuario_admin(), _env("FLOW_DB_ADMIN_PASSWORD"), "FLOW_DB_ADMIN_PASSWORD")
