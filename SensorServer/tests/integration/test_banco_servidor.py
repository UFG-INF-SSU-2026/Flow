"""Servidor <-> banco (banco falso em memoria): leituras em lote, dupla escrita, identidade do token e resiliencia."""
import os

from borda import server
from borda import const as const_borda
from comum import const as const_comum
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
    return banco.analise().ler_leituras_novas(0)


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
        original = repositorios.borda().gravar_leituras
        banco_fora = {"sim": True}

        def instavel(lote):
            if banco_fora["sim"]:
                raise ConnectionError("banco fora")
            return original(lote)
        monkeypatch.setattr(repositorios.borda(), "gravar_leituras", instavel)
        for n in range(3):
            assert cli.post("/dados", _app(n=n), tokens["idoso"]).status_code == 200
        server._descarregar_leituras()                     # falha: nao levanta, o lote volta para a fila
        assert [l["payload"]["n"] for l in server._fila_leituras] == [0, 1, 2]
        banco_fora["sim"] = False                          # o banco volta
        repositorios._borda.disjuntor.sucesso()            # fim da pausa do disjuntor
        server._descarregar_leituras()
        assert [l["payload"]["n"] for l in _leituras(banco)] == [0, 1, 2] and server._fila_leituras == []

    def test_sem_banco_instalado_nada_e_enfileirado(self, cli, tokens):
        repositorios.desinstalar()
        assert cli.post("/dados", _app(), tokens["idoso"]).status_code == 200
        assert server._fila_leituras == []


# ----------------------------- telemetria e auditoria (legado) ------------------------
class TestDuplaEscrita:
    def test_flush_grava_telemetria_no_banco_e_no_arquivo(self, cli, tokens, banco):
        cli.post("/dados", _app(), tokens["idoso"])
        server._flush_iteracao()
        (t,) = banco.tabelas["telemetria"]
        assert t["user_id"] == 1 and t["wokwi"] is None and t["app"]["device_id"] == "cel1"
        assert os.path.exists(os.path.join(const_comum.DATA_DIR, "usuario_1.jsonl"))

    def test_sem_arquivos_so_o_banco(self, cli, tokens, banco, monkeypatch):
        monkeypatch.setattr(const_comum, "GRAVAR_ARQUIVOS", False)
        cli.post("/dados", _app(), tokens["idoso"])
        server._flush_iteracao()
        cli.post("/dados", _app(), None)                   # 401: auditoria
        assert len(banco.tabelas["telemetria"]) == 1 and len(banco.tabelas["auditoria"]) == 1
        assert not os.path.exists(os.path.join(const_comum.DATA_DIR, "usuario_1.jsonl"))
        assert not os.path.exists(os.path.join(const_comum.DATA_DIR, "auditoria.jsonl"))

    def test_telemetria_legado_pode_ser_desligada(self, cli, tokens, banco, monkeypatch):
        monkeypatch.setattr(const_borda, "GRAVAR_TELEMETRIA_LEGADO", False)
        cli.post("/dados", _app(), tokens["idoso"])
        server._flush_iteracao()
        assert banco.tabelas["telemetria"] == []

    def test_negacao_vai_para_a_auditoria_do_banco(self, cli, banco):
        assert cli.post("/dados", _app(), None).status_code == 401
        (a,) = banco.tabelas["auditoria"]
        assert (a["rota"], a["status"], a["motivo"]) == ("/dados", 401, "token_ausente")

    def test_estado_do_wokwi_e_gravado_uma_vez_enquanto_nao_muda(self, cli, tokens, banco, pipeline):
        cli.post("/dados", _wokwi(seq=1), tokens["dispositivo"])
        cli.post("/dados", _wokwi(seq=2), tokens["dispositivo"])
        (e,) = banco.tabelas["estado_dispositivo"]
        assert (e["user_id"], e["device_id"]) == (1, "bridge-dev") and e["estado"]["device_id"] == "bridge-dev"

# --------------------------------- /eventos ----------------------------------
class TestRespostas:
    def test_resposta_do_app_vai_ao_banco_com_a_identidade_do_token(self, cli, tokens, banco):
        assert cli.post("/eventos", _retorno(message_id="m-1"), tokens["idoso"]).status_code == 200
        (r,) = banco.tabelas["respostas_dispositivo"]
        assert (r["user_id"], r["device_id"], r["fonte"], r["tipo"], r["message_id"]) == (1, "cel1", "app",
                                                                                          "push.ack", "m-1")
        assert r["schema_version"] == const_comum.SCHEMA_VERSION_APP and r["payload"]["state"] == "ATIVADO"

    def test_resposta_do_wokwi_usa_o_device_do_token(self, cli, tokens, banco):
        corpo = {"schemaVersion": const_comum.SCHEMA_VERSION_WOKWI, "source": "wokwi", "user_id": 1, "deviceId": "bridge-dev",
                 "entityId": "idoso-sim", "eventTimeMs": 1000, "sequence": 1, "eventType": "push.ack",
                 "state": "ATIVADO", "messageId": "m-2"}
        cli.post("/eventos", corpo, tokens["dispositivo"])
        (r,) = banco.tabelas["respostas_dispositivo"]
        assert (r["fonte"], r["device_id"]) == ("wokwi", "bridge-dev")

    def test_evento_invalido_ou_de_versao_futura_nao_e_gravado(self, cli, tokens, banco):
        cli.post("/eventos", _retorno(state="ESTADO_QUE_NAO_EXISTE"), tokens["idoso"])
        cli.post("/eventos", _retorno(schemaVersion=99), tokens["idoso"])
        assert banco.tabelas["respostas_dispositivo"] == []

    def test_fora_de_escopo_nao_e_gravado(self, cli, tokens, banco):
        assert cli.post("/eventos", _retorno(user_id=2), tokens["idoso"]).status_code == 403
        assert banco.tabelas["respostas_dispositivo"] == []


def test_disjuntor_pausa_as_escritas_apos_uma_falha(banco, monkeypatch):
    chamadas = []

    def quebra(_r):
        chamadas.append(1)
        raise ConnectionError("fora")
    assert repositorios.tentar_borda("x", quebra) is False
    assert repositorios.tentar_borda("x", quebra) is False         # pausado: nem tenta
    assert len(chamadas) == 1
