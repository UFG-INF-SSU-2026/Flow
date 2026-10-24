"""POST /dados e GET /dados, /estado/<id> pelo test_client: telemetria do app e do wokwi, versao, descarte e isolamento de falhas."""
import time

import pytest
from comum import const as const_comum
from analise import nucleo
from analise import regra_queda
from borda import server

pytestmark = pytest.mark.usefixtures("pipeline")     # a analise roda em memoria apos cada POST


def _app(**extra) -> dict:
    corpo = {"source": "app", "user_id": 1, "device_id": "cel1", "timestamp_envio": int(time.time() * 1000),
             "acelerometro": {"magnitude": 9.8, "magnitude_max_janela": 9.9}}
    corpo.update(extra)
    return corpo


def _wokwi(seq=1, **extra) -> dict:
    corpo = {"schemaVersion": const_comum.SCHEMA_VERSION_WOKWI, "source": "wokwi", "user_id": 1, "eventType": "imobilidade.leitura",
             "deviceId": "bridge-dev", "entityId": "idoso-sim", "eventTimeMs": seq * 1000, "sequence": seq,
             "value": 0.3, "valueMax": 0.4, "unit": "m/s2", "state": "NORMAL"}
    corpo.update(extra)
    return corpo


@pytest.fixture
def chamadas_de_avaliar(monkeypatch):
    """Conta quantas vezes a regra de queda foi alimentada."""
    chamadas = []
    original = regra_queda.avaliar

    def espia(*args, **kwargs):
        chamadas.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(regra_queda, "avaliar", espia)
    return chamadas


# ------------------------------ telemetria valida ----------------------------
class TestTelemetriaValida:
    def test_app_vira_uma_leitura_gravada(self, cli, tokens, banco):
        r = cli.post("/dados", _app(), tokens["idoso"])
        assert r.status_code == 200 and r.get_json() == {"status": "ok", "user_id": 1, "source": "app"}
        (l,) = banco.tabelas["leituras"]
        assert l["user_id"] == 1 and l["fonte"] == "app"
        assert l["payload"]["device_id"] == "cel1" and "source" not in l["payload"] and "user_id" not in l["payload"]

    def test_wokwi_vira_uma_leitura_gravada(self, cli, tokens, banco):
        r = cli.post("/dados", _wokwi(), tokens["dispositivo"])
        assert r.status_code == 200 and r.get_json()["source"] == "wokwi"
        (l,) = banco.tabelas["leituras"]
        assert l["fonte"] == "wokwi" and l["payload"]["deviceId"] == "bridge-dev"

    def test_as_duas_fontes_do_mesmo_usuario_viram_duas_leituras(self, cli, tokens, banco):
        cli.post("/dados", _app(), tokens["idoso"])
        cli.post("/dados", _wokwi(), tokens["dispositivo"])
        assert sorted(l["fonte"] for l in banco.tabelas["leituras"]) == ["app", "wokwi"]


# ------------------------------- schemaVersion -------------------------------
class TestSchemaVersion:
    @pytest.mark.parametrize("versao", [const_comum.SCHEMA_VERSION_APP + 1, 99, 0, -1, "3", 3.0, True, None, [3]],
                             ids=repr)
    def test_versao_maior_ou_invalida_e_descartada_com_200(self, cli, tokens, banco, versao):
        r = cli.post("/dados", _app(schemaVersion=versao), tokens["idoso"])
        assert r.status_code == 200
        assert r.get_json()["descartado"] is True
        assert server._fila_leituras == [] and banco.tabelas["leituras"] == []

    def test_versao_menor_e_aceita(self, cli, tokens, banco):
        r = cli.post("/dados", _app(schemaVersion=const_comum.SCHEMA_VERSION_APP - 1), tokens["idoso"])
        assert r.status_code == 200 and "descartado" not in r.get_json()
        assert len(banco.tabelas["leituras"]) == 1

    def test_versao_ausente_e_aceita(self, cli, tokens):
        r = cli.post("/dados", _app(), tokens["idoso"])
        assert r.status_code == 200 and "descartado" not in r.get_json()

    def test_versao_atual_e_aceita(self, cli, tokens):
        r = cli.post("/dados", _app(schemaVersion=const_comum.SCHEMA_VERSION_APP), tokens["idoso"])
        assert "descartado" not in r.get_json()

    def test_a_versao_verificada_nao_vai_para_o_payload_gravado(self, cli, tokens, banco):
        cli.post("/dados", _app(), tokens["idoso"])
        assert "schemaVersion" not in banco.tabelas["leituras"][0]["payload"]


# --------------------------------- sistema.erro ------------------------------
def test_sistema_erro_do_wokwi_da_200_e_nao_gera_alerta(cli, tokens, chamadas_de_avaliar, banco):
    r = cli.post("/dados", _wokwi(eventType="sistema.erro", state="MPU_NAO_ENCONTRADO"), tokens["dispositivo"])
    assert r.status_code == 200
    assert chamadas_de_avaliar == []
    assert banco.tabelas["alertas"] == []
    assert regra_queda._episodios == {}


# ---------------------- evento wokwi invalido ou duplicado -------------------
class TestWokwiInvalidoOuDuplicado:
    def test_evento_invalido_da_200_e_nao_chega_a_regra_de_queda(self, cli, tokens, chamadas_de_avaliar):
        for ruim in (_wokwi(eventType="outro"), _wokwi(state="NAO_EXISTE"), _wokwi(value=500.0),
                     _wokwi(sequence="1"), {k: v for k, v in _wokwi().items() if k != "entityId"}):
            assert cli.post("/dados", ruim, tokens["dispositivo"]).status_code == 200
        assert chamadas_de_avaliar == []
        assert regra_queda._episodios == {}

    def test_evento_duplicado_nao_chega_a_regra_de_queda(self, cli, tokens, chamadas_de_avaliar):
        for _ in range(3):
            assert cli.post("/dados", _wokwi(seq=5), tokens["dispositivo"]).status_code == 200
        assert len(chamadas_de_avaliar) == 1                     # so a primeira
        assert cli.post("/dados", _wokwi(seq=4), tokens["dispositivo"]).status_code == 200   # atrasado
        assert len(chamadas_de_avaliar) == 1
        assert cli.post("/dados", _wokwi(seq=6), tokens["dispositivo"]).status_code == 200
        assert len(chamadas_de_avaliar) == 2

    def test_evento_aceito_chega_a_regra_de_queda(self, cli, tokens, chamadas_de_avaliar):
        cli.post("/dados", _wokwi(), tokens["dispositivo"])
        assert len(chamadas_de_avaliar) == 1

    def test_falha_inesperada_no_fluxo_legado_do_wokwi_conta_como_nao_aceito(self, cli, tokens, monkeypatch,
                                                                              chamadas_de_avaliar):
        def quebra(*a, **k):
            raise RuntimeError("bug")
        monkeypatch.setattr(nucleo, "_regra_de_imobilidade", quebra)
        assert cli.post("/dados", _wokwi(), tokens["dispositivo"]).status_code == 200
        assert chamadas_de_avaliar == []


# --------------------------- fluxo legado do app -----------------------------
class TestFluxoLegadoDoApp:
    def test_falha_no_fluxo_legado_nao_impede_a_analise_de_queda(self, cli, tokens, monkeypatch,
                                                                  chamadas_de_avaliar):
        def quebra(*a, **k):
            raise RuntimeError("bug no fluxo de luz")
        monkeypatch.setattr(nucleo, "_regra_de_luz", quebra)
        r = cli.post("/dados", _app(), tokens["idoso"])
        assert r.status_code == 200 and r.get_json()["status"] == "ok"
        assert len(chamadas_de_avaliar) == 1

    def test_falha_ao_avaliar_a_queda_nao_vira_500_e_a_telemetria_e_gravada(self, cli, tokens, monkeypatch, banco):
        def quebra(*a, **k):
            raise RuntimeError("bug na regra de queda")
        monkeypatch.setattr(nucleo, "_regra_de_queda", quebra)
        assert cli.post("/dados", _app(), tokens["idoso"]).status_code == 200
        assert len(banco.tabelas["leituras"]) == 1

    def test_payload_do_app_malformado_no_fluxo_legado_da_200(self, cli, tokens):
        r = cli.post("/dados", _app(leitura_ambiente={"device_id": "cel1"}), tokens["idoso"])   # faltam campos
        assert r.status_code == 200


# ------------------------------ rotas de leitura -----------------------------
class TestRotasDeLeitura:
    def test_get_dados_so_para_admin(self, cli, tokens):
        cli.post("/dados", _app(), tokens["idoso"])
        r = cli.get("/dados", tokens["admin"])
        assert r.status_code == 200 and len(r.get_json()) == 1
        for papel in ("idoso", "dispositivo"):
            assert cli.get("/dados", tokens[papel]).status_code == 403
        assert cli.get("/dados", None).status_code == 401

    def test_get_estado_so_para_admin(self, cli, tokens):
        cli.post("/dados", _wokwi(), tokens["dispositivo"])
        r = cli.get("/estado/bridge-dev", tokens["admin"])
        assert r.status_code == 200
        assert r.get_json()["device_id"] == "bridge-dev" and r.get_json()["user_id"] == 1
        for papel in ("idoso", "dispositivo"):
            assert cli.get("/estado/bridge-dev", tokens[papel]).status_code == 403
        assert cli.get("/estado/bridge-dev", None).status_code == 401

    def test_get_estado_de_device_desconhecido_da_404(self, cli, tokens):
        assert cli.get("/estado/nao-existe", tokens["admin"]).status_code == 404
