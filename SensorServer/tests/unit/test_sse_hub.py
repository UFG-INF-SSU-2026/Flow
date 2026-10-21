"""sse_hub.py: Last-Event-ID, anel de replay, filas por conexao, ids e o gerador da resposta SSE.

Sem rede e sem threads: o Hub e usado direto e o gerador da resposta e consumido com next().
Cada teste usa um Hub proprio (o HUB global fica fora).
"""
import time

import pytest

from borda import const as const_borda
from borda import sse_hub
from borda.sse_hub import Hub, ler_last_event_id

CHAVE = (1, "app")


@pytest.fixture
def hub():
    return Hub()


def msg(n):
    return {"n": n}


def conectar(hub, device="cel1", ultimo=0, chave=CHAVE):
    return hub._registrar(chave, device, ultimo, None)


def esvaziar(conn):
    itens = []
    while not conn.fila.empty():
        itens.append(conn.fila.get_nowait())
    return itens


# --------------------------- Last-Event-ID -----------------------------------
@pytest.mark.parametrize("valor,esperado", [(None, 0), ("", 0), ("abc", 0), ("-5", 0), ("3.5", 0),
                                            ("7", 7), ("0", 0), (" 12 ", 12), ("1e3", 0)])
def test_ler_last_event_id(valor, esperado):
    assert ler_last_event_id(valor) == esperado


# ------------------------------- publicar ------------------------------------
class TestPublicar:
    def test_sem_conexao_devolve_zero_e_guarda_no_anel(self, hub):
        assert hub.publicar(1, "app", msg(1)) == 0
        assert [m for _, m in hub._anel[CHAVE]] == [msg(1)]

    def test_com_n_conexoes_devolve_n_e_guarda_no_anel(self, hub):
        conns = [conectar(hub, f"d{i}") for i in range(3)]
        assert hub.publicar(1, "app", msg(1)) == 3
        assert [m for _, m in hub._anel[CHAVE]] == [msg(1)]
        assert all(len(esvaziar(c)) == 1 for c in conns)

    def test_so_entrega_a_chave_certa(self, hub):
        do_app = conectar(hub, "a", chave=(1, "app"))
        do_wokwi = conectar(hub, "w", chave=(1, "wokwi"))
        de_outro = conectar(hub, "x", chave=(2, "app"))
        assert hub.publicar(1, "app", msg(1)) == 1
        assert len(esvaziar(do_app)) == 1 and not esvaziar(do_wokwi) and not esvaziar(de_outro)

    def test_ha_conexao(self, hub):
        assert hub.ha_conexao(1, "app") is False
        conectar(hub)
        assert hub.ha_conexao(1, "app") is True and hub.ha_conexao(1, "wokwi") is False

    def test_anel_respeita_tamanho_anel(self, hub, monkeypatch):
        monkeypatch.setattr(const_borda, "TAMANHO_ANEL", 3)
        for i in range(5):
            hub.publicar(1, "app", msg(i))
        assert [m["n"] for _, m in hub._anel[CHAVE]] == [2, 3, 4]


# ----------------------------------- ids -------------------------------------
class TestIds:
    def test_ids_crescem_de_um_em_um_acima_da_base(self, hub):
        for i in range(3):
            hub.publicar(1, "app", msg(i))
        assert [eid for eid, _ in hub._anel[CHAVE]] == [hub._base_ids + 1, hub._base_ids + 2, hub._base_ids + 3]

    def test_ids_por_usuario_e_canal_sao_independentes(self, hub):
        hub.publicar(1, "app", msg(1))
        hub.publicar(1, "app", msg(2))
        hub.publicar(1, "wokwi", msg(3))
        hub.publicar(2, "app", msg(4))
        primeiro = lambda chave: hub._anel[chave][0][0]   # noqa: E731
        assert primeiro((1, "wokwi")) == primeiro((2, "app")) == primeiro((1, "app")) == hub._base_ids + 1

    def test_base_e_pelo_menos_o_instante_de_criacao(self):
        antes = int(time.time())
        assert Hub()._base_ids >= antes


# ------------------------------- replay --------------------------------------
class TestReplay:
    def test_sem_last_event_id_recebe_tudo_do_anel_em_ordem(self, hub):
        for i in range(3):
            hub.publicar(1, "app", msg(i))
        itens = esvaziar(conectar(hub))
        assert [m["n"] for _, m in itens] == [0, 1, 2]
        assert [e for e, _ in itens] == sorted(e for e, _ in itens)

    def test_com_last_event_id_recebe_so_o_que_faltou(self, hub):
        for i in range(4):
            hub.publicar(1, "app", msg(i))
        ultimo_visto = hub._base_ids + 2
        itens = esvaziar(conectar(hub, ultimo=ultimo_visto))
        assert [m["n"] for _, m in itens] == [2, 3]

    def test_last_event_id_do_ultimo_nao_recebe_nada(self, hub):
        hub.publicar(1, "app", msg(1))
        assert esvaziar(conectar(hub, ultimo=hub._base_ids + 1)) == []

    def test_replay_e_truncado_pelos_mais_recentes(self, hub, monkeypatch):
        monkeypatch.setattr(const_borda, "TAMANHO_FILA_CONEXAO", 3)
        for i in range(6):
            hub.publicar(1, "app", msg(i))
        itens = esvaziar(conectar(hub))
        assert [m["n"] for _, m in itens] == [3, 4, 5]

    def test_replay_nao_vaza_para_outro_canal(self, hub):
        hub.publicar(1, "wokwi", msg(1))
        assert esvaziar(conectar(hub, chave=(1, "app"))) == []


# ---------------------- fila cheia e substituicao ----------------------------
class TestConexoes:
    def test_fila_cheia_fecha_remove_e_nao_conta_como_entregue(self, hub, monkeypatch):
        monkeypatch.setattr(const_borda, "TAMANHO_FILA_CONEXAO", 2)
        lenta = conectar(hub, "lenta")
        saudavel = conectar(hub, "ok")
        assert hub.publicar(1, "app", msg(1)) == 2
        assert hub.publicar(1, "app", msg(2)) == 2
        esvaziar(saudavel)                                  # so a "ok" consome
        assert hub.publicar(1, "app", msg(3)) == 1          # a "lenta" estourou: nao conta
        assert "lenta" not in hub._conns[CHAVE] and "ok" in hub._conns[CHAVE]
        assert lenta.fila.get_nowait() is sse_hub._FIM      # fechar() descartou o pendente e pos o marcador

    def test_apos_fila_cheia_o_replay_traz_o_que_expirou_da_fila(self, hub, monkeypatch):
        monkeypatch.setattr(const_borda, "TAMANHO_FILA_CONEXAO", 2)
        conectar(hub, "lenta")
        for i in range(3):
            hub.publicar(1, "app", msg(i))
        itens = esvaziar(conectar(hub, "lenta"))
        assert [m["n"] for _, m in itens] == [1, 2]

    def test_nova_conexao_do_mesmo_device_substitui_a_anterior(self, hub):
        antiga = conectar(hub, "cel1")
        nova = conectar(hub, "cel1")
        assert hub._conns[CHAVE]["cel1"] is nova
        assert antiga.fila.get_nowait() is sse_hub._FIM
        assert hub.publicar(1, "app", msg(1)) == 1

    def test_devices_diferentes_convivem(self, hub):
        conectar(hub, "a")
        conectar(hub, "b")
        assert set(hub._conns[CHAVE]) == {"a", "b"}

    def test_remover_ignora_conexao_ja_substituida(self, hub):
        antiga = conectar(hub, "cel1")
        nova = conectar(hub, "cel1")
        hub._remover(CHAVE, antiga)
        assert hub._conns[CHAVE]["cel1"] is nova


# ----------------------- gerador de resposta_sse -----------------------------
def serializar(m):
    return f"dados-{m['n']}"


def abrir(hub, exp_token=None, ultimo=0, ser=serializar, device="cel1"):
    exp = time.time() + 3600 if exp_token is None else exp_token
    resp = hub.resposta_sse(1, "app", device, exp, ultimo, ser)
    return resp, iter(resp.response)


class TestRespostaSse:
    def test_cabecalhos(self, hub):
        resp, _ = abrir(hub)
        assert resp.mimetype == "text/event-stream"
        assert resp.headers["Cache-Control"] == "no-cache" and resp.headers["X-Accel-Buffering"] == "no"
        assert "Connection" not in resp.headers

    def test_nada_e_registrado_antes_da_primeira_iteracao(self, hub):
        abrir(hub)
        assert not hub.ha_conexao(1, "app")

    def test_abre_com_comentario_conectado_e_registra(self, hub):
        _, gen = abrir(hub)
        assert next(gen) == ": conectado\n\n"
        assert hub.ha_conexao(1, "app")
        gen.close()

    def test_fechar_o_gerador_remove_a_conexao(self, hub):
        _, gen = abrir(hub)
        next(gen)
        gen.close()
        assert not hub.ha_conexao(1, "app")

    def test_mensagem_publicada_sai_com_id_evento_e_dados(self, hub):
        _, gen = abrir(hub)
        next(gen)
        hub.publicar(1, "app", msg(7))
        assert next(gen) == f"id: {hub._base_ids + 1}\nevent: push\ndata: dados-7\n\n"
        gen.close()

    def test_replay_sai_em_ordem_crescente(self, hub):
        for i in range(3):
            hub.publicar(1, "app", msg(i))
        _, gen = abrir(hub)
        next(gen)
        ids = [int(next(gen).split("\n")[0][4:]) for _ in range(3)]
        assert ids == sorted(ids) and len(set(ids)) == 3
        gen.close()

    def test_mensagem_expirada_e_descartada(self, hub):
        hub.publicar(1, "app", msg(1))
        hub.publicar(1, "app", msg(2))
        _, gen = abrir(hub, ser=lambda m: None if m["n"] == 1 else f"dados-{m['n']}")
        next(gen)
        assert "dados-2" in next(gen)          # a 1 foi pulada sem emitir nada
        gen.close()

    def test_keepalive_quando_a_fila_esta_vazia(self, hub, monkeypatch):
        monkeypatch.setattr(const_borda, "KEEPALIVE_S", 0.01)
        _, gen = abrir(hub)
        next(gen)
        assert next(gen) == ": keepalive\n\n"
        gen.close()

    def test_perto_do_exp_emite_reautenticar_e_encerra(self, hub):
        _, gen = abrir(hub, exp_token=time.time() + const_borda.AVISO_REAUTENTICAR_S - 1)
        assert next(gen) == ": conectado\n\n"
        assert next(gen) == "event: reautenticar\ndata: {}\n\n"
        with pytest.raises(StopIteration):
            next(gen)
        assert not hub.ha_conexao(1, "app")     # o finally remove a conexao

    def test_conexao_substituida_encerra_o_stream_antigo(self, hub):
        _, gen_antigo = abrir(hub)
        next(gen_antigo)
        _, gen_novo = abrir(hub)
        next(gen_novo)
        with pytest.raises(StopIteration):
            next(gen_antigo)
        assert hub.ha_conexao(1, "app")         # a nova segue registrada
        gen_novo.close()
