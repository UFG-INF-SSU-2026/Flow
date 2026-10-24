"""
Acesso unico da borda ao repositorio de banco.

  iniciar()    -> abre o pool e VERIFICA a versao do esquema; falha com mensagem clara (a borda nao sobe).
  usar_fake()  -> instala um banco falso novo em memoria (testes e `FLOW_DB_FAKE=1`). Devolve o BancoFalso.
  repo()       -> o repositorio instalado, ou None.
  desinstalar() -> fecha o que estiver aberto.

Quem usa NUNCA toca em SQL nem em pool.
"""
_repo = None
_banco_falso = None


def iniciar() -> None:
    """Levanta RuntimeError (mensagem sem senha) se o banco faltar ou estiver defasado."""
    global _repo
    from flow_db import config, conexao, versao
    from flow_db.pg import Pg
    try:
        pool = conexao.criar_pool()
        try:
            conexao.verificar_versao_minima(pool, versao.MIGRACAO_MINIMA)
        except Exception:
            pool.close()
            raise
    except (config.ConfigBanco, conexao.BancoIndisponivel, conexao.BancoDesatualizado) as e:
        raise RuntimeError(str(e)) from None
    _repo = Pg(pool)


def usar_fake():
    global _repo, _banco_falso
    from flow_db.fake import BancoFalso
    _banco_falso = BancoFalso()
    _repo = _banco_falso
    return _banco_falso


def banco_falso():
    return _banco_falso


def repo():
    return _repo


def desinstalar() -> None:
    global _repo
    if _repo is not None:
        try:
            _repo.fechar()
        except Exception:  # noqa: BLE001
            pass
    _repo = None
