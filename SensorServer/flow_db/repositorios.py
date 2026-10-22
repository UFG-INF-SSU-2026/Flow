"""
Acesso unico do servidor aos repositorios de banco (borda e analise).

Enquanto borda e analise forem o mesmo processo, os dois lados vivem aqui, cada um com o seu papel no banco e o seu
disjuntor. Quem usa (borda/server.py, borda/authz.py, borda/caminho_direto.py, analise/efeitos.py) NUNCA toca em SQL nem em pool: chama borda()/analise() ou
tentar_borda()/tentar_analise().

  iniciar()    -> processo unico: sobe os dois pools e VERIFICA a versao do esquema; falha com mensagem clara (o servidor
                  nao sobe). Com FLOW_DB_FAKE=1 usa o banco falso em memoria (sem Docker).
  iniciar_borda() / iniciar_analise() -> S5, dois processos: cada servico abre SO o seu pool (o fake nao vale aqui).
  usar_fake()  -> instala um banco falso novo (testes). Devolve o BancoFalso.
  tentar_*()   -> executa uma escrita sem NUNCA levantar excecao: falha vai para o log e aciona o disjuntor.
                  Devolve True se gravou. O fluxo principal (aceitar dados, responder ao dispositivo) nao depende disto.
"""
import os

from comum import const as const_comum
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
        lado.disjuntor = Disjuntor(const_comum.DB_PAUSA_APOS_FALHA_S)


def _recusar_fake_isolado(servico: str) -> None:
    if os.environ.get("FLOW_DB_FAKE") == "1":
        raise RuntimeError(f"FLOW_DB_FAKE=1 so vale no processo unico (o banco falso vive na memoria de um processo): "
                           f"o servico '{servico}' isolado precisa do PostgreSQL.")


def _abrir(servico: str):
    """Abre o pool do papel do servico e VERIFICA a versao do esquema. Devolve o repositorio PG. Levanta
    RuntimeError (mensagem sem senha). Se a verificacao falhar, o pool aberto e fechado."""
    from flow_db import config, conexao, versao
    from flow_db.pg_analise import PgAnalise
    from flow_db.pg_borda import PgBorda
    papel, classe = ((config.PAPEL_BORDA, PgBorda) if servico == "borda" else (config.PAPEL_ANALISE, PgAnalise))
    try:
        pool = conexao.criar_pool(papel)
        try:
            conexao.verificar_versao_minima(pool, versao.MIGRACAO_MINIMA[servico], servico)
        except Exception:
            pool.close()
            raise
    except (config.ConfigBanco, conexao.BancoIndisponivel, conexao.BancoDesatualizado) as e:
        raise RuntimeError(str(e)) from None
    return classe(pool)


def iniciar() -> None:
    """Processo UNICO: abre os dois lados. Levanta RuntimeError (mensagem sem senha) se o banco faltar ou estiver defasado."""
    if os.environ.get("FLOW_DB_FAKE") == "1":
        usar_fake()
        return
    repo_borda = _abrir("borda")
    try:
        repo_analise = _abrir("analise")
    except Exception:
        repo_borda.fechar()
        raise
    _zerar_disjuntores()
    _borda.repo = repo_borda
    _analise.repo = repo_analise


def iniciar_borda() -> None:
    """S5, processo da BORDA: abre SO o pool do papel flow_borda (o papel da analise nem e lido). tentar_analise()
    vira no-op. Mesmas falhas de iniciar()."""
    _recusar_fake_isolado("borda")
    repo = _abrir("borda")
    _zerar_disjuntores()
    _borda.repo = repo


def iniciar_analise() -> None:
    """S5, processo da ANALISE: abre SO o pool do papel flow_analise. tentar_borda() vira no-op."""
    _recusar_fake_isolado("analise")
    repo = _abrir("analise")
    _zerar_disjuntores()
    _analise.repo = repo


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
        print(f"  [db:{lado.nome}] falha ao gravar {nome} (pausa de {const_comum.DB_PAUSA_APOS_FALHA_S}s): "
              f"{type(e).__name__}: {str(e)[:200]}")
        return False
    lado.disjuntor.sucesso()
    return True


def tentar_borda(nome: str, fn) -> bool:
    return _tentar(_borda, nome, fn)


def tentar_analise(nome: str, fn) -> bool:
    return _tentar(_analise, nome, fn)
