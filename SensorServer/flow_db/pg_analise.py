"""RepoAnalise sobre PostgreSQL (papel flow_analise). SQL parametrizado."""
from contextlib import contextmanager
from datetime import datetime
from typing import Sequence

from ._pg import PgBase, _clausula_instante, jsonb
from .repo_analise import RepoAnalise


class PgAnalise(PgBase, RepoAnalise):
    def __init__(self, pool, conn=None, relogio=None):
        super().__init__(pool, conn)
        self._relogio = relogio       # injetavel nos testes; None = relogio real

    def _agora(self):
        return self._relogio() if self._relogio else RepoAnalise._agora(self)

    def _ler_novas(self, tabela: str, coluna_instante: str, depois_de_id: int, instante_minimo, limite: int) -> list[dict]:
        params: list = [depois_de_id]
        filtro = _clausula_instante(coluna_instante, instante_minimo, params)
        params.append(limite)
        with self._cx() as conn:
            return conn.execute(f"SELECT * FROM {tabela} WHERE id > %s{filtro} ORDER BY id LIMIT %s", params).fetchall()

    def ler_leituras_novas(self, depois_de_id, instante_minimo=None, limite=500):
        return self._ler_novas("leituras", "instante_ingestao", depois_de_id, instante_minimo, limite)

    def ler_respostas_novas(self, depois_de_id, instante_minimo=None, limite=500):
        return self._ler_novas("respostas_dispositivo", "instante_ingestao", depois_de_id, instante_minimo, limite)

    def ler_entregas_novas(self, depois_de_id, instante_minimo=None, limite=500):
        return self._ler_novas("entregas_push", "instante", depois_de_id, instante_minimo, limite)

    def ler_leituras_recentes(self, ate_id: int, instante_minimo: datetime, limite: int = 5000) -> list[dict]:
        # As mais recentes primeiro (DESC + LIMIT) e a ordem crescente de volta; o filtro de instante deixa o Timescale
        # excluir os chunks antigos.
        with self._cx() as conn:
            return conn.execute(
                "SELECT * FROM (SELECT * FROM leituras WHERE id <= %s AND instante_ingestao >= %s"
                " ORDER BY id DESC LIMIT %s) recentes ORDER BY id", (ate_id, instante_minimo, limite)).fetchall()

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

    def gravar_comando(self, c: dict) -> int:
        with self._cx() as conn:
            return conn.execute(
                "INSERT INTO comandos_push (criado_em, user_id, device_id, canal, message_id, tipo, payload, expira_em,"
                " episodio_id, leitura_id_origem) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (c["criado_em"], c["user_id"], c.get("device_id"), c["canal"], c["message_id"], c["tipo"],
                 jsonb(c["payload"]), c.get("expira_em"), c.get("episodio_id"), c.get("leitura_id_origem"))
            ).fetchone()["id"]

    def gravar_alerta(self, a: dict) -> int:
        with self._cx() as conn:
            return conn.execute(
                "INSERT INTO alertas (instante, user_id, device_id, tipo, mensagem, dados, episodio_id,"
                " leitura_id_origem, schema_version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (a["instante"], a["user_id"], a.get("device_id"), a["tipo"], a.get("mensagem"),
                 jsonb(a.get("dados", {})), a.get("episodio_id"), a.get("leitura_id_origem"), a.get("schema_version"))
            ).fetchone()["id"]

    def gravar_confirmacao(self, c: dict) -> int:
        with self._cx() as conn:
            return conn.execute(
                "INSERT INTO confirmacoes (instante, user_id, tipo, dados, episodio_id, message_id,"
                " leitura_id_origem, schema_version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (c["instante"], c["user_id"], c["tipo"], jsonb(c.get("dados", {})), c.get("episodio_id"),
                 c.get("message_id"), c.get("leitura_id_origem"), c.get("schema_version"))
            ).fetchone()["id"]

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

    def listar_episodios_abertos(self) -> list[dict]:
        with self._cx() as conn:
            return conn.execute("SELECT * FROM episodios WHERE estado <> 'encerrado' ORDER BY aberto_em").fetchall()

    def gravar_estado_dispositivo(self, s: dict) -> None:
        with self._cx() as conn:
            conn.execute(
                "INSERT INTO estado_dispositivo (user_id, device_id, atualizado_em, versao_regra, estado)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (user_id, device_id) DO UPDATE SET"
                " atualizado_em = EXCLUDED.atualizado_em, versao_regra = EXCLUDED.versao_regra,"
                " estado = EXCLUDED.estado",
                (s["user_id"], s["device_id"], s["atualizado_em"], s["versao_regra"], jsonb(s["estado"])))

    def ler_cursor(self) -> dict:
        with self._cx() as conn:
            return conn.execute("SELECT * FROM cursor_analise WHERE singleton").fetchone()

    def gravar_cursor(self, ultimo_leitura_id=None, ultimo_leitura_instante=None, ultimo_resposta_id=None,
                      ultimo_entrega_id=None, versao_codigo=None, metricas=None) -> None:
        with self._cx() as conn:
            conn.execute(
                "UPDATE cursor_analise SET"
                " ultimo_leitura_id = COALESCE(%s, ultimo_leitura_id),"
                " ultimo_leitura_instante = COALESCE(%s, ultimo_leitura_instante),"
                " ultimo_resposta_id = COALESCE(%s, ultimo_resposta_id),"
                " ultimo_entrega_id = COALESCE(%s, ultimo_entrega_id),"
                " versao_codigo = COALESCE(%s, versao_codigo),"
                " metricas = COALESCE(%s::jsonb, metricas),"
                " atualizado_em = now() WHERE singleton",
                (ultimo_leitura_id, ultimo_leitura_instante, ultimo_resposta_id, ultimo_entrega_id, versao_codigo,
                 jsonb(metricas) if metricas is not None else None))

    @contextmanager
    def transacao(self):
        if self._conn is not None:      # ja dentro de uma transacao: reaproveita
            yield self
            return
        with self._pool.connection() as conn:   # commit ao sair; rollback se a excecao subir
            yield PgAnalise(self._pool, conn, self._relogio)
