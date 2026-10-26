"""Borda <-> banco (banco falso em memoria): leituras em lote, identidade do token, estado e resiliencia a queda do banco e da analise."""
from datetime import datetime, timezone

from borda import server
from borda import const as const_borda
from comum import const as const_comum
from borda import cliente_analise
from flow_db import repositorios


def _app(**extra) -> dict:
    corpo = {"source": "app", "user_id": 1, "device_id": "cel1", "timestamp_envio": 1_760_000_000_000,
             "acelerometro": {"magnitude": 9.8, "magnitude_max_janela": 9.9}}
    corpo.update(extra)
    return corpo


def _wokwi(seq=1, **extra) -> dict:
    corpo = {"schemaVersion": const_comum.SCHEMA_VERSION_WOKWI, "source": "wokwi", "user_id": 1,
             "eventType": "imobilidade.leitura", "deviceId": "bridge-dev", "entityId": "idoso-sim",
             "eventTimeMs": seq * 1000, "sequence": seq, "value": 0.3, "valueMax": 0.4, "unit": "m/s2",
             "state": "NORMAL"}
    corpo.update(extra)
    return corpo


def _retorno(tipo="push.ack", state="ATIVADO", message_id="m-1", **extra) -> dict:
    corpo = {"schemaVersion": const_comum.SCHEMA_VERSION_APP, "source": "app", "user_id": 1, "device_id": "cel1",
             "event_time": "2026-10-14T12:00:00.000Z", "eventType": tipo, "state": state, "messageId": message_id}
    corpo.update(extra)
    return corpo


def _leituras(banco):
    return banco.ler_leituras_recentes(datetime(2000, 1, 1, tzinfo=timezone.utc))


# ------------------------------- leituras em lote ----------------------------
class TestLeituras:
    def test_dados_do_app_viram_uma_leitura_com_identidade_do_token(self, cli, tokens, banco):
        assert cli.post("/dados", _app(), tokens["idoso"]).status_code == 200
        assert _leituras(banco) == []                      # nada vai ao banco dentro da requisicao
        server._descarregar_leituras()
        (l,) = _leituras(banco)
        assert (l["user_id"], l["device_id"], l["fonte"], l["conta_id"]) == (1, "cel1", "app", "idoso1")
        assert l["schema_version"] == const_comum.SCHEMA_VERSION_PADRAO       # payload sem schemaVersion vale a versao padrao
        assert l["tipo_evento"] == "telemetria" and l["instante_origem"].timestamp() == 1_760_000_000
        assert "source" not in l["payload"] and "user_id" not in l["payload"] and l["payload"]["device_id"] == "cel1"

    def test_dados_do_wokwi_guardam_o_tipo_do_evento(self, cli, tokens, banco):
        cli.post("/dados", _wokwi(), tokens["dispositivo"])
        server._descarregar_leituras()
        (l,) = _leituras(banco)
        assert (l["fonte"], l["tipo_evento"], l["conta_id"], l["instante_origem"]) == ("wokwi", "imobilidade.leitura",
                                                                                          "bridge1", None)

    def test_dado_descartado_por_versao_nao_vira_leitura(self, cli, tokens, banco):
        cli.post("/dados", _app(schemaVersion=99), tokens["idoso"])
        server._descarregar_leituras()
        assert _leituras(banco) == []

    def test_descarga_respeita_o_tamanho_do_lote_e_esvazia_a_fila(self, cli, tokens, banco, monkeypatch):
        monkeypatch.setattr(const_borda, "TAMANHO_LOTE_LEITURAS", 2)
        for n in range(5):
            cli.post("/dados", _app(n=n), tokens["idoso"])
        server._descarregar_leituras()
        assert [l["payload"]["n"] for l in _leituras(banco)] == [0, 1, 2, 3, 4] and server._fila_leituras == []

    def test_fila_cheia_descarta_as_mais_antigas_e_conta(self, cli, tokens, banco, monkeypatch):
        monkeypatch.setattr(const_borda, "MAX_FILA_LEITURAS", 3)
        for n in range(5):
            assert cli.post("/dados", _app(n=n), tokens["idoso"]).status_code == 200
        assert [l["payload"]["n"] for l in server._fila_leituras] == [2, 3, 4] and server._descartes_leituras == 2

    def test_com_o_banco_fora_a_borda_aceita_guarda_e_depois_grava_na_ordem(self, cli, tokens, banco, monkeypatch):
        original = repositorios.repo().gravar_leituras
        banco_fora = {"sim": True}
        durante = []

        def instavel(lote):
            durante.append(server._estatisticas_borda()["fila_leituras"])    # o que o /health mostraria na tentativa
            if banco_fora["sim"]:
                raise ConnectionError("banco fora")
            return original(lote)
        monkeypatch.setattr(repositorios.repo(), "gravar_leituras", instavel)
        for n in range(3):
            assert cli.post("/dados", _app(n=n), tokens["idoso"]).status_code == 200
        server._descarregar_leituras()                     # falha: nao levanta, o lote volta para a fila
        assert [l["payload"]["n"] for l in server._fila_leituras] == [0, 1, 2]
        assert durante == [3] and server._estatisticas_borda()["fila_leituras"] == 3    # o lote em tentativa conta, sem duplicar
        assert server._fila_analise == []                  # nada foi a analise: o banco nao confirmou
        banco_fora["sim"] = False                          # o banco volta
        server._descarregar_leituras()
        assert [l["payload"]["n"] for l in _leituras(banco)] == [0, 1, 2] and server._fila_leituras == []
        assert server._estatisticas_borda()["fila_leituras"] == 0

    def test_sem_banco_instalado_nada_e_enfileirado(self, cli, tokens):
        repositorios.desinstalar()
        assert cli.post("/dados", _app(), tokens["idoso"]).status_code == 200
        assert server._fila_leituras == []


class TestEstado:
    def test_estado_do_wokwi_e_gravado_uma_vez_enquanto_nao_muda(self, cli, tokens, banco, pipeline):
        cli.post("/dados", _wokwi(seq=1), tokens["dispositivo"])
        cli.post("/dados", _wokwi(seq=2), tokens["dispositivo"])
        (e,) = banco.tabelas["estado_dispositivo"]
        assert (e["user_id"], e["device_id"]) == (1, "bridge-dev") and e["estado"]["device_id"] == "bridge-dev"


# --------------------------------- /eventos ----------------------------------
class TestRespostas:
    def test_resposta_do_app_vai_a_fila_da_analise_com_a_identidade_do_token(self, cli, tokens):
        assert cli.post("/eventos", _retorno(message_id="m-1"), tokens["idoso"]).status_code == 200
        (r,) = server._fila_analise
        assert (r.user_id, r.device_id, r.fonte) == (1, "cel1", "app")
        assert r.evento["eventType"] == "push.ack" and r.evento["messageId"] == "m-1" and r.evento["state"] == "ATIVADO"
        assert r.schema_version == const_comum.SCHEMA_VERSION_APP

    def test_resposta_do_wokwi_usa_o_device_do_token(self, cli, tokens):
        corpo = {"schemaVersion": const_comum.SCHEMA_VERSION_WOKWI, "source": "wokwi", "user_id": 1, "deviceId": "bridge-dev",
                 "entityId": "idoso-sim", "eventTimeMs": 1000, "sequence": 1, "eventType": "push.ack",
                 "state": "ATIVADO", "messageId": "m-2"}
        cli.post("/eventos", corpo, tokens["dispositivo"])
        (r,) = server._fila_analise
        assert (r.fonte, r.device_id) == ("wokwi", "bridge-dev")

    def test_evento_invalido_ou_de_versao_futura_nao_vai_a_analise(self, cli, tokens):
        cli.post("/eventos", _retorno(state="ESTADO_QUE_NAO_EXISTE"), tokens["idoso"])
        cli.post("/eventos", _retorno(schemaVersion=99), tokens["idoso"])
        assert server._fila_analise == []

    def test_fora_de_escopo_nao_vai_a_analise(self, cli, tokens):
        assert cli.post("/eventos", _retorno(user_id=2), tokens["idoso"]).status_code == 403
        assert server._fila_analise == []


# ------------------------------ a analise cai e volta -----------------------------
class TestAnaliseForaDoAr:
    def test_borda_segue_aceitando_e_gravando_e_o_health_acusa(self, cli, tokens, banco, pipeline):
        pipeline.fora_do_ar = True
        for seq in (1, 2):
            assert cli.post("/dados", _wokwi(seq=seq), tokens["dispositivo"]).status_code == 200
        assert len(_leituras(banco)) == 2                  # gravadas mesmo sem a analise
        assert len(server._fila_analise) == 2              # esperando
        server.passo_vigia()
        corpo = cli.get("/health").get_json()
        assert corpo["status"] == "ok" and corpo["analise"]["estado"] == "degradado" and corpo["analise"]["fila"] == 2
        assert corpo["analise"]["ultima_resposta_ha_s"] >= 0       # respondeu na subida (restauracao), depois parou

    def test_ao_voltar_a_analise_recebe_a_fila_e_o_health_normaliza(self, cli, tokens, banco, pipeline):
        pipeline.fora_do_ar = True
        cli.post("/dados", _wokwi(seq=1), tokens["dispositivo"])
        pipeline.fora_do_ar = False
        pipeline.drenar()
        assert server._fila_analise == []
        assert cli.get("/health").get_json()["analise"]["estado"] == "ok"
        assert banco.tabelas["estado_dispositivo"]

    def test_analise_reiniciada_e_restaurada_antes_de_seguir(self, cli, tokens, banco, pipeline):
        cli.post("/dados", _wokwi(seq=1), tokens["dispositivo"])
        antigo = pipeline.analise.boot_id
        pipeline.reiniciar_analise(estado=None)
        assert pipeline.analise.boot_id != antigo
        cli.post("/dados", _wokwi(seq=2), tokens["dispositivo"])
        assert ("POST", "/restaurar") in pipeline.chamadas[2:]       # alem da restauracao da subida

    def test_a_analise_recusar_o_lote_nao_trava_a_fila(self, cli, tokens, monkeypatch, pipeline):
        def recusa(*_a, **_k):
            raise cliente_analise.RequisicaoRecusada("/analisar")
        monkeypatch.setattr(pipeline.analise, "analisar", recusa)
        cli.post("/dados", _wokwi(seq=1), tokens["dispositivo"])
        assert server._fila_analise == []

    def test_fila_de_espera_cheia_descarta_as_mais_antigas(self, cli, tokens, monkeypatch, pipeline):
        monkeypatch.setattr(const_borda, "MAX_FILA_ANALISE", 2)
        pipeline.fora_do_ar = True
        for seq in range(1, 5):
            cli.post("/dados", _wokwi(seq=seq), tokens["dispositivo"])
        assert len(server._fila_analise) == 2 and server._descartes_analise == 2


class TestBancoForaComDecisoes:
    def test_decisao_nao_e_aplicada_nem_o_push_sai_enquanto_o_banco_nao_confirma(self, cli, tokens, banco, pipeline,
                                                                                  relogio, monkeypatch):
        original = banco.gravar_episodio
        fora = {"sim": True}

        def instavel(e):
            if fora["sim"]:
                raise ConnectionError("banco fora")
            return original(e)
        monkeypatch.setattr(banco, "gravar_episodio", instavel)
        entregas = []
        server._aplicador._canais = [type("C", (), {"CANAL": "x", "enviar": staticmethod(lambda u, m: entregas.append(m) or "ENTREGUE_AO_CANAL")})]
        for seq, (v, vmax) in enumerate([(1.0, 1.5), (3.0, 25.0), (0.3, 0.4), (0.3, 0.4), (0.3, 0.4)], start=1):
            cli.post("/dados", _wokwi(seq=seq, value=v, valueMax=vmax), tokens["dispositivo"])
            relogio.avancar(1)
        assert entregas == [] and banco.tabelas["comandos_push"] == []
        assert server._aplicador.pendentes > 0             # a decisao ficou guardada
        fora["sim"] = False
        pipeline.drenar()
        assert len(banco.tabelas["comandos_push"]) == 1 and len(entregas) == 1 and server._aplicador.pendentes == 0
