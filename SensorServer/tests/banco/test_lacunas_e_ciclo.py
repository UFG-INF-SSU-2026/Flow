"""Item 3.6 contra o Postgres real: regra de lacunas de id e processar_lote (saida + cursor atomicos).

Espelha tests/unit/test_flow_db_lacunas.py e test_flow_db_ciclo_analise.py. O relogio da analise e injetado
(sem sleep): os instantes de ingestao sao fixos em T0 e o relogio decide se o buraco e "recente" ou "permanente".
"""
from datetime import datetime, timedelta, timezone

import pytest

from flow_db import config
from flow_db.ciclo_analise import processar_lote
from flow_db.pg_analise import PgAnalise

pytestmark = pytest.mark.banco

T0 = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
ESPERA = config.ESPERA_LACUNA_S
_INSERT = ("INSERT INTO leituras (instante_ingestao, user_id, device_id, fonte, tipo_evento, schema_version,"
           " instante_origem, conta_id, payload) VALUES (%s, 1, 'cel1', 'app', 'telemetria', 3, NULL, 'idoso1', '{}')")


class Relogio:
    def __init__(self):
        self.agora = T0

    def __call__(self):
        return self.agora

    def avancar(self, s):
        self.agora += timedelta(seconds=s)


@pytest.fixture
def relogio():
    return Relogio()


@pytest.fixture
def leitor(pools, admin, relogio):
    return PgAnalise(pools[1], relogio=relogio)


@pytest.fixture
def conexao_borda():
    """Uma conexao propria do papel flow_borda, sem autocommit: permite deixar uma transacao aberta."""
    import psycopg
    with psycopg.connect(config.dsn_papel(config.PAPEL_BORDA)) as conn:
        yield conn


def inserir(conn, seg=0):
    conn.execute(_INSERT, (T0 + timedelta(seconds=seg),))


def ids(linhas):
    return [l["id"] for l in linhas]


class TestLacunasReais:
    def test_ids_contiguos(self, borda, leitor):
        borda.gravar_leituras([_leitura(i) for i in range(3)])
        linhas, lacunas = leitor.ler_leituras_seguras(0)
        assert len(linhas) == 3 and lacunas == 0

    # Antes de abrir a transacao "A", uma linha ja confirmada cria o chunk do hypertable: o primeiro INSERT num chunk
    # novo o cria dentro da transacao, e um segundo escritor ficaria esperando o commit (statement_timeout).

    def test_commit_fora_de_ordem_nao_expoe_o_id_maior_antes_do_menor(self, conexao_borda, borda, leitor):
        borda.gravar_leituras([_leitura(0)])            # id 1, confirmado (cria o chunk)
        inserir(conexao_borda, 1)                       # A: reserva o id 2 e fica sem commit
        borda.gravar_leituras([_leitura(2)])            # B: id 3 confirmado
        linhas, lacunas = leitor.ler_leituras_seguras(0)
        assert ids(linhas) == [1] and lacunas == 0      # o 3 ja esta visivel, o 2 nao: espera
        conexao_borda.commit()                          # A confirma
        linhas, lacunas = leitor.ler_leituras_seguras(0)
        assert ids(linhas) == [1, 2, 3] and lacunas == 0

    def test_rollback_cria_buraco_permanente(self, conexao_borda, borda, leitor, relogio):
        borda.gravar_leituras([_leitura(0)])            # id 1 (cria o chunk)
        inserir(conexao_borda, 1)                       # A: id 2
        borda.gravar_leituras([_leitura(2)])            # B: id 3
        conexao_borda.rollback()                        # o id 2 nunca volta
        linhas, lacunas = leitor.ler_leituras_seguras(0)
        assert ids(linhas) == [1] and lacunas == 0                 # antes do prazo: espera
        relogio.avancar(60)
        linhas, lacunas = leitor.ler_leituras_seguras(0)
        assert ids(linhas) == [1, 3] and lacunas == 1              # depois do prazo: devolve e registra

    def test_instante_minimo_continua_descartando_chunks_antigos(self, borda, leitor, relogio):
        borda.gravar_leituras([_leitura(i) for i in range(4)])
        relogio.avancar(60)
        linhas, _ = leitor.ler_leituras_seguras(0, instante_minimo=T0 + timedelta(seconds=2))
        assert [l["payload"]["x"] for l in linhas] == [2, 3]


class TestProcessarLoteReal:
    @staticmethod
    def _alerta(leitura_id):
        return {"instante": T0, "user_id": 1, "device_id": "cel1", "tipo": "queda", "mensagem": "m",
                "leitura_id_origem": leitura_id, "schema_version": 3}

    def _gravar(self, tx, linhas):
        for l in linhas:
            tx.gravar_alerta(self._alerta(l["id"]))

    def test_lote_processado_avanca_o_cursor(self, borda, leitor, admin):
        borda.gravar_leituras([_leitura(i) for i in range(3)])
        assert processar_lote(leitor, self._gravar) == 3
        assert [r["leitura_id_origem"] for r in admin.execute("SELECT * FROM alertas ORDER BY id").fetchall()] == [1, 2, 3]
        assert leitor.ler_cursor()["ultimo_leitura_id"] == 3

    def test_falha_forcada_deixa_o_cursor_onde_estava_e_reexecucao_grava_uma_vez(self, borda, leitor, admin):
        import psycopg
        borda.gravar_leituras([_leitura(i) for i in range(3)])

        def segunda_saida_invalida(tx, linhas):
            tx.gravar_alerta(self._alerta(linhas[0]["id"]))
            tx.gravar_comando({"criado_em": T0, "user_id": 1, "canal": "invalido", "message_id": "m1", "tipo": "t",
                               "payload": {}})          # viola o CHECK de canal

        with pytest.raises(psycopg.errors.CheckViolation):
            processar_lote(leitor, segunda_saida_invalida)
        assert admin.execute("SELECT count(*) AS n FROM alertas").fetchone()["n"] == 0
        assert leitor.ler_cursor()["ultimo_leitura_id"] == 0

        assert processar_lote(leitor, self._gravar) == 3
        assert admin.execute("SELECT count(*) AS n FROM alertas").fetchone()["n"] == 3
        assert leitor.ler_cursor()["ultimo_leitura_id"] == 3

    def test_lote_vazio_renova_o_batimento(self, leitor, admin):
        admin.execute("UPDATE cursor_analise SET atualizado_em = now() - interval '1 hour'")
        antes = leitor.ler_cursor()
        assert processar_lote(leitor, self._gravar) == 0
        depois = leitor.ler_cursor()
        assert depois["ultimo_leitura_id"] == 0 and depois["atualizado_em"] > antes["atualizado_em"]

    def test_buraco_recente_avanca_so_ate_o_buraco_e_depois_conta(self, conexao_borda, borda, leitor, relogio, admin):
        borda.gravar_leituras([_leitura(0)])            # id 1
        inserir(conexao_borda, 1)                       # id 2, sem commit
        borda.gravar_leituras([_leitura(2)])            # id 3
        assert processar_lote(leitor, self._gravar) == 1
        assert leitor.ler_cursor()["ultimo_leitura_id"] == 1
        conexao_borda.rollback()                        # buraco permanente
        assert processar_lote(leitor, self._gravar) == 0
        relogio.avancar(60)
        assert processar_lote(leitor, self._gravar) == 1
        c = leitor.ler_cursor()
        assert c["ultimo_leitura_id"] == 3 and c["metricas"]["lacunas_ignoradas"] == 1


def _leitura(seg):
    return {"instante_ingestao": T0 + timedelta(seconds=seg), "user_id": 1, "device_id": "cel1", "fonte": "app",
            "tipo_evento": "telemetria", "schema_version": 3, "instante_origem": None, "conta_id": "idoso1",
            "payload": {"x": seg}}