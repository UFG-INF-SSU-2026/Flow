"""O contrato das duas camadas de acesso contra o Postgres real (espelha tests/unit/test_flow_db_fake.py)."""
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.banco

T0 = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)


def leitura(seg=0, user_id=1, fonte="app"):
    return {"instante_ingestao": T0 + timedelta(seconds=seg), "user_id": user_id, "device_id": "cel1", "fonte": fonte,
            "tipo_evento": "telemetria", "schema_version": 3, "instante_origem": None, "conta_id": "idoso1",
            "payload": {"x": seg, "acentuado": "luz inadequada ç"}}


def comando(msg="m1", seg=0):
    return {"criado_em": T0 + timedelta(seconds=seg), "user_id": 1, "device_id": None, "canal": "todos",
            "message_id": msg, "tipo": "queda.confirmacao_solicitada", "payload": {"a": 1},
            "expira_em": T0 + timedelta(seconds=seg + 7), "episodio_id": "e1"}


def episodio(**extra):
    base = {"episodio_id": "e1", "user_id": 1, "device_id": None, "tipo": "IMOBILIDADE_PROLONGADA",
            "estado": "aguardando_confirmacao", "aberto_em": T0, "atualizado_em": T0, "encerrado_em": None,
            "motivo_encerramento": None, "dados": {"timeout_s": 5}}
    base.update(extra)
    return base


def test_leituras_em_lote_ida_e_volta(borda, analise):
    assert borda.gravar_leituras([leitura(i) for i in range(3)]) == 3
    lidas = analise.ler_leituras_novas(0)
    assert [l["id"] for l in lidas] == sorted(l["id"] for l in lidas) and len(lidas) == 3
    assert lidas[0]["payload"] == {"x": 0, "acentuado": "luz inadequada ç"} and lidas[0]["conta_id"] == "idoso1"
    ultimo = lidas[0]["id"]
    assert [l["payload"]["x"] for l in analise.ler_leituras_novas(ultimo)] == [1, 2]
    assert [l["payload"]["x"] for l in analise.ler_leituras_novas(0, limite=2)] == [0, 1]
    assert [l["payload"]["x"] for l in analise.ler_leituras_novas(0, instante_minimo=T0 + timedelta(seconds=1))] == [1, 2]


def test_lote_com_linha_invalida_nao_grava_nenhuma(borda, analise):
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        borda.gravar_leituras([leitura(0), leitura(1, fonte="outra")])
    assert analise.ler_leituras_novas(0) == []


def test_respostas_e_entregas(borda, analise):
    borda.gravar_resposta({"instante_ingestao": T0, "user_id": 1, "device_id": "cel1", "fonte": "app",
                           "tipo": "push.ack", "message_id": "m1", "schema_version": 3, "payload": {"state": "ATIVADO"}})
    borda.gravar_entrega({"instante": T0, "message_id": "m1", "user_id": 1, "canal": "app", "resultado": "enviado"})
    assert analise.ler_respostas_novas(0)[0]["payload"] == {"state": "ATIVADO"}
    assert analise.ler_entregas_novas(0)[0]["resultado"] == "enviado"


def test_comando_ida_e_volta(borda, analise):
    ids = [analise.gravar_comando(comando(f"m{i}", i)) for i in range(3)]
    assert ids == sorted(ids)
    assert [c["message_id"] for c in borda.ler_comandos_novos(ids[0])] == ["m1", "m2"]


def test_alerta_e_confirmacao(borda, analise):
    analise.gravar_alerta({"instante": T0, "user_id": 1, "tipo": "imobilidade", "mensagem": "m", "dados": {"k": 1}})
    analise.gravar_confirmacao({"instante": T0, "user_id": 1, "tipo": "episodio.aberto", "dados": {}, "episodio_id": "e1"})
    (a,) = borda.listar_alertas(1)
    assert a["tipo"] == "imobilidade" and a["dados"] == {"k": 1} and a["id"] >= 1


def test_upsert_de_episodio(borda, analise):
    analise.gravar_episodio(episodio(device_id="cel1"))
    fim = T0 + timedelta(seconds=9)
    analise.gravar_episodio(episodio(tipo="DESCONHECIDO", estado="encerrado", atualizado_em=fim, encerrado_em=fim,
                                     motivo_encerramento="CONFIRMADO", device_id=None, dados={"extra": 1}))
    ep = borda.buscar_episodio("e1")
    assert (ep["estado"], ep["motivo_encerramento"], ep["encerrado_em"]) == ("encerrado", "CONFIRMADO", fim)
    assert ep["tipo"] == "IMOBILIDADE_PROLONGADA" and ep["device_id"] == "cel1"
    assert ep["dados"] == {"timeout_s": 5, "extra": 1}
    assert analise.listar_episodios_abertos() == []


def test_estado_do_dispositivo_e_upsert(borda, analise):
    base = {"user_id": 1, "device_id": "d", "atualizado_em": T0, "versao_regra": 1, "estado": {"alerta_ativo": False}}
    analise.gravar_estado_dispositivo(base)
    analise.gravar_estado_dispositivo({**base, "estado": {"alerta_ativo": True}})
    assert borda.ler_estado_dispositivo(1, "d")["estado"] == {"alerta_ativo": True}


def test_transacao_desfaz_tudo_se_algo_falha(borda, analise):
    with pytest.raises(RuntimeError):
        with analise.transacao() as tx:
            tx.gravar_episodio(episodio())
            tx.gravar_comando(comando())
            raise RuntimeError("cai no meio do lote")
    assert borda.buscar_episodio("e1") is None and borda.ler_comandos_novos(0) == []


def test_transacao_confirma_tudo_ao_sair(borda, analise):
    with analise.transacao() as tx:
        tx.gravar_episodio(episodio())
        tx.gravar_comando(comando())
        tx.gravar_cursor(ultimo_leitura_id=5)
    assert borda.buscar_episodio("e1") and len(borda.ler_comandos_novos(0)) == 1
    assert analise.ler_cursor()["ultimo_leitura_id"] == 5


def test_cursor_mantem_o_nao_informado_e_renova_o_batimento(admin, analise):
    antes = analise.ler_cursor()["atualizado_em"]
    analise.gravar_cursor(ultimo_leitura_id=7, metricas={"n": 1})
    analise.gravar_cursor(versao_codigo="v1")
    c = analise.ler_cursor()
    assert (c["ultimo_leitura_id"], c["versao_codigo"], c["metricas"]) == (7, "v1", {"n": 1})
    assert c["atualizado_em"] >= antes


def test_cursor_da_borda_e_saude_do_pipeline(borda, analise):
    borda.gravar_leituras([leitura(i) for i in range(4)])
    ids = [l["id"] for l in analise.ler_leituras_novas(0)]
    s = borda.ler_saude()
    assert s["atraso_ids"] == ids[-1] and s["idade_ultimo_comando_entregue_s"] is None   # cursor zerado: atraso = maior id
    analise.gravar_cursor(ultimo_leitura_id=ids[2])
    borda.gravar_cursor_borda(1, T0)
    s = borda.ler_saude()
    assert s["atraso_ids"] == ids[-1] - ids[2] and s["idade_batimento_analise_s"] >= 0 and s["idade_ultimo_comando_entregue_s"] > 0
    assert borda.ler_cursor_borda()["ultimo_comando_id"] == 1


def test_telemetria_e_auditoria(borda, admin):
    borda.gravar_telemetria([{"instante": T0, "user_id": 1, "app": {"a": 1}, "wokwi": None}])
    borda.gravar_auditoria({"instante": T0, "rota": "/dados", "status": 401, "motivo": "token_ausente", "ip": "1.2.3.4"})
    t = admin.execute("SELECT app, wokwi FROM telemetria").fetchone()
    assert t["app"] == {"a": 1} and t["wokwi"] is None
    assert admin.execute("SELECT motivo FROM auditoria").fetchone()["motivo"] == "token_ausente"


def test_consultar_leituras_mais_recentes_primeiro(borda):
    borda.gravar_leituras([leitura(i) for i in range(3)])
    assert [l["payload"]["x"] for l in borda.consultar_leituras(1, limite=2)] == [2, 1]


# ------------------------------------------------------------------ S6: leituras da recuperacao na subida
def test_leituras_recentes_ate_o_cursor_as_mais_recentes_em_ordem_de_id(borda, analise):
    borda.gravar_leituras([leitura(i * 10) for i in range(5)])              # t = 0, 10, 20, 30, 40
    ids = [l["id"] for l in analise.ler_leituras_novas(0)]
    recentes = analise.ler_leituras_recentes(ids[3], T0 + timedelta(seconds=10), 100)
    assert [l["payload"]["x"] for l in recentes] == [10, 20, 30]            # nao passa do cursor, nem da janela
    assert [l["payload"]["x"] for l in analise.ler_leituras_recentes(ids[3], T0, 2)] == [20, 30]   # as MAIS recentes
    assert analise.ler_leituras_recentes(0, T0, 100) == []


def test_episodios_recentes_trazem_os_abertos_de_qualquer_idade(analise):
    analise.gravar_episodio(episodio(episodio_id="velho-aberto", aberto_em=T0 - timedelta(days=2),
                                     atualizado_em=T0 - timedelta(days=2)))
    analise.gravar_episodio(episodio(episodio_id="velho-encerrado", estado="encerrado", aberto_em=T0 - timedelta(days=2),
                                     atualizado_em=T0 - timedelta(days=2), encerrado_em=T0 - timedelta(days=2),
                                     motivo_encerramento="EXPIRADO"))
    analise.gravar_episodio(episodio(episodio_id="recente-encerrado", estado="encerrado", encerrado_em=T0,
                                     motivo_encerramento="CONFIRMADO"))
    achados = analise.listar_episodios_recentes(T0 - timedelta(hours=1))
    assert [e["episodio_id"] for e in achados] == ["velho-aberto", "recente-encerrado"]


def test_comandos_de_episodios_trazem_o_message_id(analise):
    analise.gravar_comando(comando("m1", 0))
    analise.gravar_comando({**comando("m2", 1), "episodio_id": "outro"})
    achados = analise.listar_comandos_de_episodios(["e1"])
    assert [(c["episodio_id"], c["message_id"]) for c in achados] == [("e1", "m1")]
    assert analise.listar_comandos_de_episodios([]) == []
