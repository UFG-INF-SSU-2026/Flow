"""RepoBorda sobre PostgreSQL (papel flow_borda). SQL parametrizado; nenhuma string de usuario vira SQL."""
from datetime import datetime
from typing import Sequence

from ._pg import PgBase, _clausula_instante, jsonb
from .repo_borda import RepoBorda


class PgBorda(PgBase, RepoBorda):
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

    def gravar_telemetria(self, linhas: Sequence[dict]) -> int:
        if not linhas:
            return 0
        dados = [(l["instante"], l["user_id"], jsonb(l.get("app")) if l.get("app") is not None else None,
                  jsonb(l.get("wokwi")) if l.get("wokwi") is not None else None) for l in linhas]
        with self._cx() as conn, conn.cursor() as cur:
            cur.executemany("INSERT INTO telemetria (instante, user_id, app, wokwi) VALUES (%s, %s, %s, %s)", dados)
        return len(dados)

    def gravar_resposta(self, r: dict) -> None:
        with self._cx() as conn:
            conn.execute(
                "INSERT INTO respostas_dispositivo (instante_ingestao, user_id, device_id, fonte, tipo, message_id,"
                " schema_version, payload) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (r["instante_ingestao"], r["user_id"], r.get("device_id"), r["fonte"], r["tipo"], r.get("message_id"),
                 r["schema_version"], jsonb(r["payload"])))

    def gravar_entrega(self, e: dict) -> None:
        with self._cx() as conn:
            conn.execute(
                "INSERT INTO entregas_push (instante, comando_id, message_id, user_id, device_id, canal, resultado,"
                " detalhe) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (e["instante"], e.get("comando_id"), e.get("message_id"), e["user_id"], e.get("device_id"),
                 e["canal"], e["resultado"], e.get("detalhe")))

    def gravar_auditoria(self, a: dict) -> None:
        with self._cx() as conn:
            conn.execute("INSERT INTO auditoria (instante, rota, status, motivo, conta, ip) VALUES (%s, %s, %s, %s, %s, %s)",
                         (a["instante"], a.get("rota"), a["status"], a["motivo"], a.get("conta"), a.get("ip")))

    def ler_comandos_novos(self, depois_de_id: int, instante_minimo: datetime | None = None,
                           limite: int = 100) -> list[dict]:
        params: list = [depois_de_id]
        filtro = _clausula_instante("criado_em", instante_minimo, params)
        params.append(limite)
        with self._cx() as conn:
            return conn.execute(f"SELECT * FROM comandos_push WHERE id > %s{filtro} ORDER BY id LIMIT %s",
                                params).fetchall()

    def ler_cursor_borda(self) -> dict:
        with self._cx() as conn:
            return conn.execute("SELECT ultimo_comando_id, ultimo_comando_instante, atualizado_em"
                                " FROM cursor_borda WHERE singleton").fetchone()

    def gravar_cursor_borda(self, ultimo_comando_id: int, ultimo_comando_instante: datetime | None) -> None:
        with self._cx() as conn:
            conn.execute("UPDATE cursor_borda SET ultimo_comando_id = %s, ultimo_comando_instante = %s,"
                         " atualizado_em = now() WHERE singleton", (ultimo_comando_id, ultimo_comando_instante))

    def consultar_leituras(self, user_id: int, limite: int = 100) -> list[dict]:
        with self._cx() as conn:
            return conn.execute("SELECT * FROM leituras WHERE user_id = %s ORDER BY instante_ingestao DESC LIMIT %s",
                                (user_id, limite)).fetchall()

    def listar_alertas(self, user_id: int, limite: int = 50) -> list[dict]:
        with self._cx() as conn:
            return conn.execute("SELECT * FROM alertas WHERE user_id = %s ORDER BY instante DESC LIMIT %s",
                                (user_id, limite)).fetchall()

    def buscar_episodio(self, episodio_id: str) -> dict | None:
        with self._cx() as conn:
            return conn.execute("SELECT * FROM episodios WHERE episodio_id = %s", (episodio_id,)).fetchone()

    def ler_estado_dispositivo(self, user_id: int, device_id: str) -> dict | None:
        with self._cx() as conn:
            return conn.execute("SELECT * FROM estado_dispositivo WHERE user_id = %s AND device_id = %s",
                                (user_id, device_id)).fetchone()

    def ler_estado_por_dispositivo(self, device_id: str) -> dict | None:
        with self._cx() as conn:
            return conn.execute("SELECT * FROM estado_dispositivo WHERE device_id = %s ORDER BY atualizado_em DESC LIMIT 1",
                                (device_id,)).fetchone()

    def ler_saude(self) -> dict:
        with self._cx() as conn:
            return conn.execute("SELECT atraso_ids, idade_batimento_analise_s, idade_ultimo_comando_entregue_s"
                                " FROM saude_pipeline").fetchone()
