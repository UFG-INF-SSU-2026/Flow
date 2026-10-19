"""Apoio comum das implementacoes PostgreSQL."""
import json
from contextlib import contextmanager


def jsonb(valor):
    """Adapta um objeto Python para jsonb. `default=str` evita quebrar a gravacao por um tipo nao serializavel."""
    from psycopg.types.json import Jsonb
    return Jsonb(valor, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))


class PgBase:
    """Conexao vem do pool (uma por operacao) ou esta fixa numa transacao (`conn`)."""

    def __init__(self, pool, conn=None):
        self._pool = pool
        self._conn = conn

    @contextmanager
    def _cx(self):
        if self._conn is not None:
            yield self._conn
        else:
            with self._pool.connection() as conn:   # commit ao sair; rollback se levantar excecao
                yield conn

    def fechar(self) -> None:
        if self._conn is None and self._pool is not None:
            self._pool.close()


def _clausula_instante(coluna: str, instante_minimo, params: list) -> str:
    if instante_minimo is None:
        return ""
    params.append(instante_minimo)
    return f" AND {coluna} >= %s"
