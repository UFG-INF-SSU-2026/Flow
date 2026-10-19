"""processar_lote: saida + cursor na mesma transacao (item 3.6). Banco falso; o espelho real esta em tests/banco."""
from datetime import timedelta

import pytest

from flow_db.ciclo_analise import processar_lote
from flow_db.fake import BancoFalso
from tests.unit.test_flow_db_lacunas import ESPERA, T0, Relogio, abrir_buraco, ids, leitura


@pytest.fixture
def relogio():
    return Relogio()


@pytest.fixture
def bd(relogio):
    return BancoFalso(relogio=relogio)


@pytest.fixture
def borda(bd):
    return bd.borda()


@pytest.fixture
def analise(bd):
    return bd.analise()


def alerta(leitura_id):
    return {"instante": T0, "user_id": 1, "device_id": "cel1", "tipo": "queda", "mensagem": "m",
            "leitura_id_origem": leitura_id, "schema_version": 3}


def gravar_alertas(tx, linhas):
    for l in linhas:
        tx.gravar_alerta(alerta(l["id"]))


def test_lote_processado_grava_saidas_e_avanca_o_cursor(borda, analise, bd):
    borda.gravar_leituras([leitura(i) for i in range(3)])
    assert processar_lote(analise, gravar_alertas) == 3
    assert [a["leitura_id_origem"] for a in bd.tabelas["alertas"]] == [1, 2, 3]
    c = analise.ler_cursor()
    assert c["ultimo_leitura_id"] == 3 and c["ultimo_leitura_instante"] == T0 + timedelta(seconds=2)
    assert processar_lote(analise, gravar_alertas) == 0            # nada novo: nao reprocessa
    assert len(bd.tabelas["alertas"]) == 3


def test_excecao_no_meio_desfaz_saidas_e_cursor_e_reexecucao_grava_uma_vez(borda, analise, bd):
    borda.gravar_leituras([leitura(i) for i in range(3)])

    def falha_na_metade(tx, linhas):
        tx.gravar_alerta(alerta(linhas[0]["id"]))
        tx.gravar_comando({"criado_em": T0, "user_id": 1, "canal": "todos", "message_id": "m1", "tipo": "t",
                           "payload": {}})
        raise RuntimeError("falhou no meio do lote")

    with pytest.raises(RuntimeError):
        processar_lote(analise, falha_na_metade)
    assert bd.tabelas["alertas"] == [] and bd.tabelas["comandos_push"] == []
    assert analise.ler_cursor()["ultimo_leitura_id"] == 0
    assert bd._ids["alertas"] == 0 and bd._ids["comandos_push"] == 0     # ids tambem revertidos

    assert processar_lote(analise, gravar_alertas) == 3                  # mesmo lote reprocessado
    assert [a["id"] for a in bd.tabelas["alertas"]] == [1, 2, 3]          # uma vez so
    assert analise.ler_cursor()["ultimo_leitura_id"] == 3


def test_lote_vazio_nao_move_o_cursor_mas_renova_o_batimento(relogio, analise):
    antes = analise.ler_cursor()
    relogio.avancar(30)
    assert processar_lote(analise, gravar_alertas) == 0
    depois = analise.ler_cursor()
    assert depois["ultimo_leitura_id"] == antes["ultimo_leitura_id"] == 0
    assert depois["atualizado_em"] == antes["atualizado_em"] + timedelta(seconds=30)


def test_buraco_recente_avanca_so_ate_o_buraco(bd, relogio, borda, analise):
    borda.gravar_leituras([leitura(i) for i in range(4)])
    abrir_buraco(bd, 3)
    assert processar_lote(analise, gravar_alertas) == 2
    assert analise.ler_cursor()["ultimo_leitura_id"] == 2
    assert processar_lote(analise, gravar_alertas) == 0                  # espera o buraco
    assert analise.ler_cursor()["ultimo_leitura_id"] == 2

    relogio.avancar(ESPERA + 5)                                          # buraco vira permanente
    assert processar_lote(analise, gravar_alertas) == 1
    c = analise.ler_cursor()
    assert c["ultimo_leitura_id"] == 4 and c["metricas"]["lacunas_ignoradas"] == 1
    assert [a["leitura_id_origem"] for a in bd.tabelas["alertas"]] == [1, 2, 4]


def test_lacunas_acumulam_e_preservam_outras_metricas(bd, relogio, borda, analise):
    analise.gravar_cursor(metricas={"outra": 7, "lacunas_ignoradas": 5})
    borda.gravar_leituras([leitura(i) for i in range(3)])
    abrir_buraco(bd, 2)
    relogio.avancar(60)
    processar_lote(analise, gravar_alertas)
    assert analise.ler_cursor()["metricas"] == {"outra": 7, "lacunas_ignoradas": 6}


def test_limite_do_lote(borda, analise):
    borda.gravar_leituras([leitura(i) for i in range(5)])
    assert processar_lote(analise, gravar_alertas, limite=2) == 2
    assert analise.ler_cursor()["ultimo_leitura_id"] == 2
    assert processar_lote(analise, gravar_alertas, limite=10) == 3
