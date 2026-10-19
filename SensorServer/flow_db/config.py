"""
Configuracao de conexao. So le variaveis de ambiente (o .env e carregado por const.py ou por flow_db/__main__.py).

Variaveis:
    FLOW_DB_HOST (127.0.0.1)  FLOW_DB_PORT (5432)  FLOW_DB_NAME (flow)
    FLOW_DB_USER (flow)       FLOW_DB_PASSWORD      <- ADMINISTRATIVO: so migracoes, provisionamento e backup
    FLOW_DB_BORDA_PASSWORD                          <- senha do papel flow_borda   (so o servico de borda a conhece)
    FLOW_DB_ANALISE_PASSWORD                        <- senha do papel flow_analise (so o servico de analise a conhece)

Senhas dos papeis: somente letras, numeros, '_' e '-' (minimo 16). Nenhuma senha aparece em log ou em mensagem de erro.
"""
import os
import re

PAPEL_BORDA = "flow_borda"
PAPEL_ANALISE = "flow_analise"
PAPEIS = (PAPEL_BORDA, PAPEL_ANALISE)

ENV_SENHA = {PAPEL_BORDA: "FLOW_DB_BORDA_PASSWORD", PAPEL_ANALISE: "FLOW_DB_ANALISE_PASSWORD"}

# Tamanho maximo do pool de cada papel. O limite de conexoes do papel no banco = pool + FOLGA_CONEXOES.
POOL_MAX = {PAPEL_BORDA: 6, PAPEL_ANALISE: 4}
FOLGA_CONEXOES = 3

# Defesa em profundidade: tempos limite definidos no proprio papel (alem dos do codigo).
TIMEOUT_CONSULTA_S = {PAPEL_BORDA: 5, PAPEL_ANALISE: 10}
TIMEOUT_TRANSACAO_OCIOSA_S = 10

# Regra de lacunas de id (leitura da analise): quanto esperar por um id ausente antes de trata-lo como buraco
# permanente. Maior que a duracao de uma transacao de lote (o statement_timeout da borda e 5 s).
ESPERA_LACUNA_S = 2.0
LIMITE_LOTE = 500

TIMEOUT_CONEXAO_S = 3        # connect_timeout do driver
TIMEOUT_POOL_S = 2.0         # espera maxima por uma conexao livre do pool
TIMEOUT_ABERTURA_POOL_S = 5.0  # espera maxima para o pool abrir na subida do servico

_SENHA_VALIDA = re.compile(r"^[A-Za-z0-9_-]{16,}$")


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


def usuario_admin() -> str:
    return _env("FLOW_DB_USER", "flow")


def limite_conexoes(papel: str) -> int:
    return POOL_MAX[papel] + FOLGA_CONEXOES


def senha_valida(senha: str | None) -> bool:
    return bool(senha) and _SENHA_VALIDA.match(senha) is not None


def senha_do_papel(papel: str) -> str:
    """Senha de um papel de servico, lida SO da variavel dele. Valida o formato sem revelar o valor."""
    nome = ENV_SENHA[papel]
    senha = _env(nome)
    if senha is None:
        raise ConfigBanco(f"{nome} nao definida (senha do papel {papel}). Veja .env.example.")
    if not senha_valida(senha):
        raise ConfigBanco(f"{nome} invalida: use so letras, numeros, '_' e '-', com no minimo 16 caracteres.")
    return senha


def _conninfo(usuario: str, senha: str, aplicacao: str) -> str:
    from psycopg.conninfo import make_conninfo
    return make_conninfo(host=host(), port=porta(), dbname=nome_banco(), user=usuario, password=senha,
                         connect_timeout=TIMEOUT_CONEXAO_S, application_name=aplicacao)


def dsn_admin() -> str:
    senha = _env("FLOW_DB_PASSWORD")
    if senha is None:
        raise ConfigBanco("FLOW_DB_PASSWORD (senha administrativa) nao definida.")
    return _conninfo(usuario_admin(), senha, "flow-admin")


def dsn_papel(papel: str) -> str:
    return _conninfo(papel, senha_do_papel(papel), f"flow-{papel}")
