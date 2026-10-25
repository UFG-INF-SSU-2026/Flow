"""Repo sobre PostgreSQL. SQL parametrizado; nenhuma string de usuario vira SQL."""
from contextlib import contextmanager
from datetime import datetime
from typing import Sequence

from ._pg import PgBase, jsonb
from .repo import Repo


class Pg(PgBase, Repo):
    def gravar_leituras(self, leituras: Sequence[dict]) -> int:
        if not leituras:
            return 0
        linhas = [(l["instante_ingestao"], l["user_id"], l["device_id"], l["fonte"], l.get("tipo_evento"),
                   l["schema_version"], l.get("instante_origem"), l.get("conta_id"), jsonb(l["payload"]))
                  for l in leituras]
        with self._cx() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO leituras (instante_ingestao, user_id, device_id, fonte, tipo_evento, schema_version,"
                " instante_origem, conta_id, payload) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)", linhas)
        return len(linhas)

    def ler_leituras_recentes(self, desde: datetime, antes_de: datetime | None = None, limite: int = 5000) -> list[dict]:
        params: list = [desde]
        filtro = ""
        if antes_de is not None:
            filtro = " AND instante_ingestao < %s"
            params.append(antes_de)
        params.append(limite)
        with self._cx() as conn:     # as mais recentes primeiro (DESC + LIMIT) e a ordem crescente de volta
            return conn.execute(
                "SELECT * FROM (SELECT * FROM leituras WHERE instante_ingestao >= %s" + filtro +
                " ORDER BY id DESC LIMIT %s) recentes ORDER BY id", params).fetchall()

    def gravar_comando(self, c: dict) -> int | None:
        with self._cx() as conn:
            linha = conn.execute(
                "INSERT INTO comandos_push (criado_em, user_id, device_id, canal, message_id, tipo, payload, expira_em,"
                " episodio_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (message_id) DO NOTHING RETURNING id",
                (c["criado_em"], c["user_id"], c.get("device_id"), c["canal"], c["message_id"], c["tipo"],
                 jsonb(c["payload"]), c.get("expira_em"), c.get("episodio_id"))).fetchone()
        return None if linha is None else linha["id"]

    def gravar_alerta(self, a: dict) -> int:
        with self._cx() as conn:
            return conn.execute(
                "INSERT INTO alertas (instante, user_id, device_id, tipo, mensagem, dados, episodio_id, schema_version)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (a["instante"], a["user_id"], a.get("device_id"), a["tipo"], a.get("mensagem"),
                 jsonb(a.get("dados", {})), a.get("episodio_id"), a.get("schema_version"))).fetchone()["id"]

    def gravar_confirmacao(self, c: dict) -> int:
        with self._cx() as conn:
            return conn.execute(
                "INSERT INTO confirmacoes (instante, user_id, tipo, dados, episodio_id, message_id, schema_version)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (c["instante"], c["user_id"], c["tipo"], jsonb(c.get("dados", {})), c.get("episodio_id"),
                 c.get("message_id"), c.get("schema_version"))).fetchone()["id"]

    def gravar_episodio(self, e: dict) -> None:
        with self._cx() as conn:
            conn.execute(
                "INSERT INTO episodios (episodio_id, user_id, device_id, tipo, estado, aberto_em, atualizado_em,"
                " encerrado_em, motivo_encerramento, dados) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (episodio_id) DO UPDATE SET"
                " estado = EXCLUDED.estado, atualizado_em = EXCLUDED.atualizado_em,"
                " encerrado_em = COALESCE(EXCLUDED.encerrado_em, episodios.encerrado_em),"
                " motivo_encerramento = COALESCE(EXCLUDED.motivo_encerramento, episodios.motivo_encerramento),"
                " device_id = COALESCE(episodios.device_id, EXCLUDED.device_id),"
                " dados = episodios.dados || EXCLUDED.dados",
                (e["episodio_id"], e["user_id"], e.get("device_id"), e["tipo"], e["estado"], e["aberto_em"],
                 e["atualizado_em"], e.get("encerrado_em"), e.get("motivo_encerramento"), jsonb(e.get("dados", {}))))

    def gravar_estado_dispositivo(self, s: dict) -> None:
        with self._cx() as conn:
            conn.execute(
                "INSERT INTO estado_dispositivo (user_id, device_id, atualizado_em, versao_regra, estado)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (user_id, device_id) DO UPDATE SET"
                " atualizado_em = EXCLUDED.atualizado_em, versao_regra = EXCLUDED.versao_regra,"
                " estado = EXCLUDED.estado",
                (s["user_id"], s["device_id"], s["atualizado_em"], s["versao_regra"], jsonb(s["estado"])))

    def ler_estado_por_dispositivo(self, device_id: str) -> dict | None:
        with self._cx() as conn:
            return conn.execute("SELECT * FROM estado_dispositivo WHERE device_id = %s ORDER BY atualizado_em DESC LIMIT 1",
                                (device_id,)).fetchone()

    def listar_episodios_recentes(self, desde: datetime) -> list[dict]:
        with self._cx() as conn:
            return conn.execute("SELECT * FROM episodios WHERE estado <> 'encerrado' OR atualizado_em >= %s"
                                " ORDER BY aberto_em", (desde,)).fetchall()

    def listar_comandos_de_episodios(self, episodio_ids: Sequence[str]) -> list[dict]:
        ids = list(episodio_ids)
        if not ids:
            return []
        with self._cx() as conn:
            return conn.execute("SELECT id, episodio_id, message_id, tipo, criado_em, expira_em FROM comandos_push"
                                " WHERE episodio_id = ANY(%s) ORDER BY id", (ids,)).fetchall()

    def criar_conta(self, c: dict) -> bool:
        with self._cx() as conn:
            linha = conn.execute(
                "INSERT INTO contas (id, papel, user_ids, fontes, hash, criada_em) VALUES (%s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (id) DO NOTHING RETURNING id",
                (c["id"], c["papel"], list(c["user_ids"]), list(c["fontes"]), c["hash"], c["criada_em"])).fetchone()
        return linha is not None

    def buscar_conta(self, conta_id: str) -> dict | None:
        with self._cx() as conn:
            return conn.execute("SELECT * FROM contas WHERE id = %s", (conta_id,)).fetchone()

    def gravar_sessao(self, s: dict) -> None:
        with self._cx() as conn:
            conn.execute("INSERT INTO sessoes (token_hash, conta_id, fonte, device_id, expira_em)"
                         " VALUES (%s, %s, %s, %s, %s)",
                         (s["token_hash"], s["conta_id"], s.get("fonte"), s.get("device_id"), s["expira_em"]))

    def consumir_sessao(self, token_hash: str) -> dict | None:
        with self._cx() as conn:
            return conn.execute("DELETE FROM sessoes WHERE token_hash = %s RETURNING *", (token_hash,)).fetchone()

    def podar_sessoes(self, agora: datetime) -> int:
        with self._cx() as conn:
            return conn.execute("DELETE FROM sessoes WHERE expira_em <= %s", (agora,)).rowcount

    @contextmanager
    def transacao(self):
        if self._conn is not None:      # ja dentro de uma transacao: reaproveita
            yield self
            return
        with self._pool.connection() as conn:   # commit ao sair; rollback se a excecao subir
            yield Pg(self._pool, conn)
