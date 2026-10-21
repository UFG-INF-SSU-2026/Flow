"""S3a contra o Postgres real: o laco le os TRES fluxos pelo papel flow_analise, avanca os cursores e as metricas
numa transacao so, respeita a regra de lacunas e NAO escreve nenhuma tabela de saida (so registra as decisoes).

Espelha tests/unit/test_laco_analise.py. O relogio e injetado no laco e no repositorio (sem sleep).
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from analise import const as const_analise
from analise import laco
from comum import const as const_comum
from comum import mensagens
from flow_db import config
from flow_db.pg_analise import PgAnalise

pytestmark = pytest.mark.banco

T0 = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)
SAIDAS = ("comandos_push", "alertas", "confirmacoes", "episodios", "estado_dispositivo")
_INSERT = ("INSERT INTO leituras (instante_ingestao, user_id, device_id, fonte, tipo_evento, schema_version,"
           " instante_origem, conta_id, payload) VALUES (%s, 1, 'cel1', 'app', 'telemetria', 3, NULL, 'idoso1', '{}')")


class Relogio:
    def __init__(self):
        self.agora = T0

    def __call__(self):
        return self.agora

    def ir_para(self, s):
        self.agora = T0 + timedelta(seconds=s)


def leitura_app(t, seq, **extra):
    return {"instante_ingestao": T0 + timedelta(seconds=t), "user_id": 1, "device_id": "cel1", "fonte": "app",
            "tipo_evento": "telemetria", "schema_version": const_comum.SCHEMA_VERSION_APP, "instante_origem": None,
            "conta_id": "idoso1",
            "payload": {"device_id": "cel1", "timestamp_envio": 1_760_000_000_000 + seq * 1000,
                        "acelerometro": {"magnitude": 9.8, "magnitude_max_janela": 9.9}}, **extra}


def leitura_wokwi(t, seq, value, vmax=None):
    p = {"schemaVersion": const_comum.SCHEMA_VERSION_WOKWI, "eventType": "imobilidade.leitura", "deviceId": "bridge-dev",
         "entityId": "idoso-sim", "eventTimeMs": seq * 1000, "sequence": seq, "value": value, "unit": "m/s2",
         "state": "NORMAL"}
    if vmax is not None:
        p["valueMax"] = vmax
    return {"instante_ingestao": T0 + timedelta(seconds=t), "user_id": 1, "device_id": "bridge-dev", "fonte": "wokwi",
            "tipo_evento": "imobilidade.leitura", "schema_version": const_comum.SCHEMA_VERSION_WOKWI,
            "instante_origem": None, "conta_id": "bridge1", "payload": p}


def queda_wokwi():
    linhas = [leitura_wokwi(0, 1, 1.0, 1.5), leitura_wokwi(1, 2, 3.0, 25.0)]
    return linhas + [leitura_wokwi(2 + k, 3 + k, 0.3, 0.4) for k in range(int(const_analise.T_POS_IMPACTO_S))]


def resposta(t, message_id):
    ev = {"eventType": "confirmacao.resposta", "state": "ESTOU_BEM", "messageId": message_id, "device_id": "cel1",
          "source": "app"}
    return {"instante_ingestao": T0 + timedelta(seconds=t), "user_id": 1, "device_id": "cel1", "fonte": "app",
            "tipo": "confirmacao.resposta", "message_id": message_id,
            "schema_version": const_comum.SCHEMA_VERSION_APP, "payload": ev}


def entrega(t, resultado="enviado"):
    return {"instante": T0 + timedelta(seconds=t), "comando_id": None, "message_id": "m1", "user_id": 1,
            "device_id": None, "canal": "app", "resultado": resultado, "detalhe": resultado}


@pytest.fixture
def mundo(pools, admin, borda):
    rel = Relogio()
    leitor = PgAnalise(pools[1], relogio=rel)
    decisoes: list = []
    lc = laco.LacoAnalise(obter_repo=lambda: leitor, relogio=rel,
                          ao_decidir=lambda d, origem, rec: decisoes.append((d, origem, rec)))
    return SimpleNamespace(rel=rel, leitor=leitor, laco=lc, decisoes=decisoes, borda=borda, admin=admin)


@pytest.fixture
def conexao_borda():
    """Conexao propria do papel flow_borda, sem autocommit: permite deixar uma transacao aberta (id reservado)."""
    import psycopg
    with psycopg.connect(config.dsn_papel(config.PAPEL_BORDA)) as conn:
        yield conn


def contar(m, tabela) -> int:
    return m.admin.execute(f"SELECT count(*) AS n FROM {tabela}").fetchone()["n"]


class TestLacoReal:
    def test_tres_fluxos_cursores_metricas_e_batimento(self, mundo):
        m = mundo
        m.borda.gravar_leituras([leitura_app(0, 1), leitura_app(1, 2)])
        m.borda.gravar_resposta(resposta(1, "desconhecida"))
        m.borda.gravar_entrega(entrega(1))
        m.borda.gravar_entrega(entrega(2, "sem_conexao"))
        m.admin.execute("UPDATE cursor_analise SET atualizado_em = now() - interval '1 hour'")
        antes = m.leitor.ler_cursor()["atualizado_em"]
        m.rel.ir_para(3)
        m.laco.executar_ciclo()
        c = m.leitor.ler_cursor()
        assert (c["ultimo_leitura_id"], c["ultimo_resposta_id"], c["ultimo_entrega_id"]) == (2, 1, 2)
        assert c["versao_codigo"] == const_analise.VERSAO_CODIGO and c["atualizado_em"] > antes
        assert c["metricas"][laco.M_LEITURAS] == 2 and c["metricas"][laco.M_RESPOSTAS] == 1
        assert c["metricas"][laco.M_ENTREGAS] == 2 and c["metricas"][laco.PREFIXO_ENTREGA + "sem_conexao"] == 1

    def test_metricas_acumulam_entre_ciclos(self, mundo):
        m = mundo
        m.borda.gravar_leituras([leitura_app(0, 1)])
        m.rel.ir_para(1)
        m.laco.executar_ciclo()
        m.borda.gravar_leituras([leitura_app(1, 2)])
        m.laco.executar_ciclo()
        assert m.leitor.ler_cursor()["metricas"][laco.M_LEITURAS] == 2

    def test_queda_e_decidida_mas_nenhuma_tabela_de_saida_e_escrita(self, mundo):
        m = mundo
        m.borda.gravar_leituras(queda_wokwi())
        m.rel.ir_para(4)
        m.laco.executar_ciclo()
        comandos = [c for d, _o, _r in m.decisoes for c in d.comandos]
        assert [c.mensagem["messageType"] for c in comandos] == [mensagens.TIPO_SOLICITADA]
        assert all(contar(m, t) == 0 for t in SAIDAS)

    def test_leitura_malformada_e_contada_e_o_cursor_avanca(self, mundo):
        m = mundo
        m.borda.gravar_leituras([leitura_app(0, 1), leitura_app(1, 2, payload=["lixo"]), leitura_app(2, 3)])
        m.rel.ir_para(3)
        m.laco.executar_ciclo()
        c = m.leitor.ler_cursor()
        assert c["ultimo_leitura_id"] == 3
        assert c["metricas"][laco.M_LEITURAS_MALFORMADAS] == 1 and c["metricas"][laco.M_LEITURAS] == 2

    def test_lacuna_recente_espera_e_a_antiga_e_contada(self, mundo, conexao_borda):
        m = mundo
        m.borda.gravar_leituras([leitura_app(0, 1)])                 # id 1 (cria o chunk)
        conexao_borda.execute(_INSERT, (T0 + timedelta(seconds=1),))   # id 2 reservado, sem commit
        m.borda.gravar_leituras([leitura_app(2, 3)])                 # id 3
        m.rel.ir_para(2.5)
        m.laco.executar_ciclo()
        assert m.leitor.ler_cursor()["ultimo_leitura_id"] == 1       # o 3 ja esta visivel, o 2 nao: espera
        conexao_borda.rollback()                                     # o id 2 nunca volta
        m.rel.ir_para(60)
        m.laco.executar_ciclo()
        c = m.leitor.ler_cursor()
        assert c["ultimo_leitura_id"] == 3 and c["metricas"]["lacunas_ignoradas"] == 1

    def test_falha_antes_do_commit_nao_avanca_nenhum_cursor(self, mundo, monkeypatch):
        m = mundo
        m.borda.gravar_leituras([leitura_app(0, 1)])
        m.borda.gravar_resposta(resposta(0, "m1"))
        m.borda.gravar_entrega(entrega(0))
        m.rel.ir_para(1)
        original = laco.ciclo_analise.processar_ciclo

        def com_falha_depois_de_processar(repo, processar, **kw):
            def processar_e_falha(tx, lote):
                processar(tx, lote)
                raise RuntimeError("falha depois de processar, antes do commit")
            return original(repo, processar_e_falha, **kw)

        monkeypatch.setattr(laco.ciclo_analise, "processar_ciclo", com_falha_depois_de_processar)
        with pytest.raises(RuntimeError):
            m.laco.executar_ciclo()
        c = m.leitor.ler_cursor()
        assert (c["ultimo_leitura_id"], c["ultimo_resposta_id"], c["ultimo_entrega_id"]) == (0, 0, 0)
        assert laco.M_LEITURAS not in c["metricas"]
        monkeypatch.undo()
        m.laco.executar_ciclo()                                      # o mesmo lote e lido de novo
        c = m.leitor.ler_cursor()
        assert (c["ultimo_leitura_id"], c["ultimo_resposta_id"], c["ultimo_entrega_id"]) == (1, 1, 1)
