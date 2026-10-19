"""
Acesso unico do servidor aos repositorios de banco (borda e analise).

Enquanto borda e analise forem o mesmo processo, os dois lados vivem aqui, cada um com o seu papel no banco e o seu
disjuntor. Quem usa (server.py, authz.py, regra_queda.py) NUNCA toca em SQL nem em pool: chama borda()/analise() ou
tentar_borda()/tentar_analise().

  iniciar()    -> sobe os pools e VERIFICA a versao do esquema; falha com mensagem clara (o servidor nao sobe).
                  Com FLOW_DB_FAKE=1 usa o banco falso em memoria (sem Docker).
  usar_fake()  -> instala um banco falso novo (testes). Devolve o BancoFalso.
  tentar_*()   -> executa uma escrita sem NUNCA levantar excecao: falha vai para o log e aciona o disjuntor.
                  Devolve True se gravou. O fluxo principal (aceitar dados, responder ao dispositivo) nao depende disto.
"""
import os

import const
from flow_db.disjuntor import Disjuntor


class _Lado:
    def __init__(self, nome: str):
        self.nome = nome
        self.repo = None
        self.disjuntor = Disjuntor(0.0)


_borda = _Lado("borda")
_analise = _Lado("analise")
_banco_falso = None


def _zerar_disjuntores() -> None:
    for lado in (_borda, _analise):
        lado.disjuntor = Disjuntor(const.DB_PAUSA_APOS_FALHA_S)


def iniciar() -> None:
    """Chamada na subida do servidor. Levanta RuntimeError (mensagem sem senha) se o banco faltar ou estiver defasado."""
    if os.environ.get("FLOW_DB_FAKE") == "1":
        usar_fake()
        return
    from flow_db import config, conexao, versao
    from flow_db.pg_analise import PgAnalise
    from flow_db.pg_borda import PgBorda
    try:
        pool_borda = conexao.criar_pool(config.PAPEL_BORDA)
        pool_analise = conexao.criar_pool(config.PAPEL_ANALISE)
        conexao.verificar_versao_minima(pool_borda, versao.MIGRACAO_MINIMA["borda"], "borda")
        conexao.verificar_versao_minima(pool_analise, versao.MIGRACAO_MINIMA["analise"], "analise")
    except (config.ConfigBanco, conexao.BancoIndisponivel, conexao.BancoDesatualizado) as e:
        raise RuntimeError(str(e)) from None
    _zerar_disjuntores()
    _borda.repo = PgBorda(pool_borda)
    _analise.repo = PgAnalise(pool_analise)


def usar_fake():
    global _banco_falso
    from flow_db.fake import BancoFalso
    _banco_falso = BancoFalso()
    _borda.repo = _banco_falso.borda()
    _analise.repo = _banco_falso.analise()
    _zerar_disjuntores()
    return _banco_falso


def banco_falso():
    return _banco_falso


def desinstalar() -> None:
    """Fecha o que estiver aberto e deixa o servidor sem banco (as escritas viram no-op)."""
    for lado in (_borda, _analise):
        if lado.repo is not None:
            try:
                lado.repo.fechar()
            except Exception:  # noqa: BLE001
                pass
        lado.repo = None


def borda():
    return _borda.repo


def analise():
    return _analise.repo


def _tentar(lado: _Lado, nome: str, fn) -> bool:
    repo = lado.repo
    if repo is None or not lado.disjuntor.permitido():
        return False
    try:
        fn(repo)
    except Exception as e:  # noqa: BLE001  (o banco nunca derruba o fluxo principal)
        lado.disjuntor.falhou()
        print(f"  [db:{lado.nome}] falha ao gravar {nome} (pausa de {const.DB_PAUSA_APOS_FALHA_S}s): "
              f"{type(e).__name__}: {str(e)[:200]}")
        return False
    lado.disjuntor.sucesso()
    return True


def tentar_borda(nome: str, fn) -> bool:
    return _tentar(_borda, nome, fn)


def tentar_analise(nome: str, fn) -> bool:
    return _tentar(_analise, nome, fn)
