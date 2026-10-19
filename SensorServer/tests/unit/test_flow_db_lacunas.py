"""Regra de lacunas de id (item 3.6): funcao pura + leitura segura no banco falso (sem Docker, sem sleep)."""
from datetime import datetime, timedelta, timezone

import pytest

from flow_db import config
from flow_db._lacunas import aplicar_regra_lacunas
from flow_db.fake import BancoFalso

T0 = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
ESPERA = config.ESPERA_LACUNA_S


class Relogio:
    def __init__(self):
        self.agora = T0

    def __call__(self):
        return self.agora

    def avancar(self, s):
        self.agora += timedelta(seconds=s)


def leitura(seg=0):
    return {"instante_ingestao": T0 + timedelta(seconds=seg), "user_id": 1, "device_id": "cel1", "fonte": "app",
            "tipo_evento": "telemetria", "schema_version": 3, "instante_origem": None, "conta_id": "idoso1",
            "payload": {"x": seg}}


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


def abrir_buraco(bd, *ids):
    """Remove linhas ja gravadas: o id some, como num ROLLBACK ou num commit ainda nao visivel."""
    bd.tabelas["leituras"] = [l for l in bd.tabelas["leituras"] if l["id"] not in ids]


def ids(linhas):
    return [l["id"] for l in linhas]


class TestFuncaoPura:
    def L(self, *ids_, seg=0):
        return [{"id": i, "instante": T0 + timedelta(seconds=seg)} for i in ids_]

    def test_vazio_e_contiguo(self):
        assert aplicar_regra_lacunas([], 0, "instante", T0, ESPERA) == ([], 0)
        linhas = self.L(6, 7, 8)
        assert aplicar_regra_lacunas(linhas, 5, "instante", T0, ESPERA) == (linhas, 0)

    def test_buraco_recente_corta_antes_do_buraco(self):
        linhas = self.L(1, 2, 5, 6)
        assert aplicar_regra_lacunas(linhas, 0, "instante", T0, ESPERA) == (linhas[:2], 0)

    def test_limite_do_prazo_e_inclusivo_no_lado_permanente(self):
        linhas = self.L(1, 3)
        agora = T0 + timedelta(seconds=ESPERA)
        assert aplicar_regra_lacunas(linhas, 0, "instante", agora, ESPERA) == (linhas, 1)
        assert aplicar_regra_lacunas(linhas, 0, "instante", agora - timedelta(milliseconds=1), ESPERA) == (linhas[:1], 0)

    def test_varios_buracos_permanentes_somam(self):
        linhas = self.L(3, 4, 8)
        assert aplicar_regra_lacunas(linhas, 0, "instante", T0 + timedelta(seconds=60), ESPERA) == (linhas, 2 + 3)

    def test_permanente_seguido_de_recente_para_no_recente(self):
        antigas = self.L(3, 4, seg=-60)
        recentes = self.L(8, 9)
        saida, ignoradas = aplicar_regra_lacunas(antigas + recentes, 0, "instante", T0, ESPERA)
        assert ids(saida) == [3, 4] and ignoradas == 2


class TestLeituraSegura:
    def test_ids_contiguos_devolvem_tudo(self, borda, analise):
        borda.gravar_leituras([leitura(i) for i in range(3)])
        linhas, lacunas = analise.ler_leituras_seguras(0)
        assert ids(linhas) == [1, 2, 3] and lacunas == 0

    def test_buraco_recente_devolve_so_o_trecho_anterior_e_e_estavel(self, bd, borda, analise):
        borda.gravar_leituras([leitura(i) for i in range(4)])       # ids 1..4
        abrir_buraco(bd, 3)
        for _ in range(3):                                           # repetir nao muda nada
            linhas, lacunas = analise.ler_leituras_seguras(0)
            assert ids(linhas) == [1, 2] and lacunas == 0

    def test_buraco_recente_logo_no_inicio_devolve_vazio(self, bd, borda, analise):
        borda.gravar_leituras([leitura(i) for i in range(3)])
        abrir_buraco(bd, 1)
        assert analise.ler_leituras_seguras(0) == ([], 0)

    def test_mesmo_buraco_depois_do_prazo_e_permanente_e_conta(self, bd, relogio, borda, analise):
        borda.gravar_leituras([leitura(i) for i in range(5)])        # ids 1..5, instantes T0..T0+4
        abrir_buraco(bd, 2, 3)
        relogio.avancar(3 + ESPERA - 0.5)                            # linha 4 (T0+3) tem 1,5 s: ainda recente
        assert ids(analise_seg(analise)) == [1]
        relogio.avancar(1)                                           # agora a linha 4 passou do prazo
        linhas, lacunas = analise.ler_leituras_seguras(0)
        assert ids(linhas) == [1, 4, 5] and lacunas == 2

    def test_buraco_entre_dois_lotes(self, bd, borda, analise):
        borda.gravar_leituras([leitura(i) for i in range(5)])
        abrir_buraco(bd, 3)
        # cursor em 2: a primeira linha do lote (id 4) nao e cursor + 1
        assert analise.ler_leituras_seguras(2) == ([], 0)

    def test_buraco_entre_dois_lotes_depois_do_prazo(self, bd, relogio, borda, analise):
        borda.gravar_leituras([leitura(0), leitura(1), leitura(2), leitura(3)])
        abrir_buraco(bd, 3)
        relogio.avancar(60)
        linhas, lacunas = analise.ler_leituras_seguras(2)
        assert ids(linhas) == [4] and lacunas == 1

    def test_limite_com_e_sem_buraco(self, bd, borda, analise):
        borda.gravar_leituras([leitura(i) for i in range(6)])
        assert ids(analise.ler_leituras_seguras(0, limite=2)[0]) == [1, 2]
        abrir_buraco(bd, 2)
        assert ids(analise.ler_leituras_seguras(0, limite=3)[0]) == [1]           # buraco dentro do lote
        assert ids(analise.ler_leituras_seguras(1, limite=2)[0]) == []            # buraco no inicio do lote

    def test_instante_minimo_continua_filtrando(self, relogio, borda, analise):
        borda.gravar_leituras([leitura(i) for i in range(4)])
        relogio.avancar(60)
        linhas, _ = analise.ler_leituras_seguras(0, instante_minimo=T0 + timedelta(seconds=2))
        assert [l["payload"]["x"] for l in linhas] == [2, 3]      # as antigas ficam de fora (nada de varredura total)

    def test_respostas_e_entregas_seguem_a_mesma_regra(self, bd, relogio, borda, analise):
        for i in range(3):
            borda.gravar_resposta({"instante_ingestao": T0, "user_id": 1, "device_id": "cel1", "fonte": "app",
                                   "tipo": "push.ack", "message_id": f"m{i}", "schema_version": 3, "payload": {}})
            borda.gravar_entrega({"instante": T0, "message_id": f"m{i}", "user_id": 1, "canal": "app",
                                  "resultado": "enviado"})
        bd.tabelas["respostas_dispositivo"] = [r for r in bd.tabelas["respostas_dispositivo"] if r["id"] != 2]
        bd.tabelas["entregas_push"] = [r for r in bd.tabelas["entregas_push"] if r["id"] != 2]
        assert ids(analise.ler_respostas_seguras(0)[0]) == [1]
        assert ids(analise.ler_entregas_seguras(0)[0]) == [1]
        relogio.avancar(ESPERA + 1)
        assert analise.ler_respostas_seguras(0) == (analise.ler_respostas_novas(0), 1)
        assert analise.ler_entregas_seguras(0) == (analise.ler_entregas_novas(0), 1)


def analise_seg(analise):
    return analise.ler_leituras_seguras(0)[0]
