"""POST /dados e GET /dados, /estado/<id> pelo test_client: telemetria do app e do wokwi, versao, descarte e isolamento de falhas."""
from borda import const as const_borda
import json
import os
import time

import pytest

from comum import const as const_comum
from analise import nucleo
from analise import regra_queda
from borda import server


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


def _linhas(user_id: int = 1) -> list[dict]:
    caminho = os.path.join(const_comum.DATA_DIR, f"usuario_{user_id}.jsonl")
    if not os.path.exists(caminho):
        return []
    with open(caminho, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


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
    def test_app_grava_linha_agregada_com_wokwi_nulo(self, cli, tokens):
        r = cli.post("/dados", _app(), tokens["idoso"])
        assert r.status_code == 200 and r.get_json() == {"status": "ok", "user_id": 1, "source": "app"}
        server._flush_iteracao()
        (linha,) = _linhas()
        assert linha["user_id"] == 1 and linha["wokwi"] is None
        assert linha["app"]["device_id"] == "cel1" and "source" not in linha["app"] and "user_id" not in linha["app"]
        assert linha["timestamp"]

    def test_wokwi_grava_linha_agregada_com_app_nulo(self, cli, tokens):
        r = cli.post("/dados", _wokwi(), tokens["dispositivo"])
        assert r.status_code == 200 and r.get_json()["source"] == "wokwi"
        server._flush_iteracao()
        (linha,) = _linhas()
        assert linha["app"] is None and linha["wokwi"]["deviceId"] == "bridge-dev"

    def test_as_duas_fontes_na_mesma_janela_viram_uma_linha(self, cli, tokens):
        cli.post("/dados", _app(), tokens["idoso"])
        cli.post("/dados", _wokwi(), tokens["dispositivo"])
        server._flush_iteracao()
        (linha,) = _linhas()
        assert linha["app"] is not None and linha["wokwi"] is not None

    def test_sem_dado_novo_nao_grava_outra_linha(self, cli, tokens):
        cli.post("/dados", _app(), tokens["idoso"])
        server._flush_iteracao()
        server._flush_iteracao()
        assert len(_linhas()) == 1

    def test_so_a_leitura_mais_recente_da_fonte_fica_na_janela(self, cli, tokens):
        cli.post("/dados", _app(device_id="cel1", timestamp_envio=1000), tokens["idoso"])
        cli.post("/dados", _app(device_id="cel1", timestamp_envio=2000), tokens["idoso"])
        server._flush_iteracao()
        (linha,) = _linhas()
        assert linha["app"]["timestamp_envio"] == 2000


# ------------------------------- schemaVersion -------------------------------
class TestSchemaVersion:
    @pytest.mark.parametrize("versao", [const_comum.SCHEMA_VERSION_APP + 1, 99, 0, -1, "3", 3.0, True, None, [3]],
                             ids=repr)
    def test_versao_maior_ou_invalida_e_descartada_com_200(self, cli, tokens, versao):
        r = cli.post("/dados", _app(schemaVersion=versao), tokens["idoso"])
        assert r.status_code == 200
        assert r.get_json()["descartado"] is True
        assert server._buffer == {}
        server._flush_iteracao()
        assert _linhas() == []

    def test_versao_menor_e_aceita(self, cli, tokens):
        r = cli.post("/dados", _app(schemaVersion=const_comum.SCHEMA_VERSION_APP - 1), tokens["idoso"])
        assert r.status_code == 200 and "descartado" not in r.get_json()
        assert 1 in server._buffer

    def test_versao_ausente_e_aceita(self, cli, tokens):
        r = cli.post("/dados", _app(), tokens["idoso"])
        assert r.status_code == 200 and "descartado" not in r.get_json()

    def test_versao_atual_e_aceita(self, cli, tokens):
        r = cli.post("/dados", _app(schemaVersion=const_comum.SCHEMA_VERSION_APP), tokens["idoso"])
        assert "descartado" not in r.get_json()

    def test_a_versao_verificada_nao_vai_para_o_arquivo(self, cli, tokens):
        cli.post("/dados", _app(), tokens["idoso"])
        server._flush_iteracao()
        assert "schemaVersion" not in _linhas()[0]["app"]


# --------------------------------- sistema.erro ------------------------------
def test_sistema_erro_do_wokwi_da_200_e_nao_gera_alerta(cli, tokens, chamadas_de_avaliar):
    r = cli.post("/dados", _wokwi(eventType="sistema.erro", state="MPU_NAO_ENCONTRADO"), tokens["dispositivo"])
    assert r.status_code == 200
    assert chamadas_de_avaliar == []
    assert not [f for f in os.listdir(const_comum.DATA_DIR) if f.startswith("alerta_")]
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

    def test_falha_ao_avaliar_a_queda_nao_vira_500_e_a_telemetria_e_gravada(self, cli, tokens, monkeypatch):
        def quebra(*a, **k):
            raise RuntimeError("bug na regra de queda")
        monkeypatch.setattr(nucleo, "_regra_de_queda", quebra)
        assert cli.post("/dados", _app(), tokens["idoso"]).status_code == 200
        server._flush_iteracao()
        assert len(_linhas()) == 1

    def test_payload_do_app_malformado_no_fluxo_legado_da_200(self, cli, tokens):
        r = cli.post("/dados", _app(leitura_ambiente={"device_id": "cel1"}), tokens["idoso"])   # faltam campos
        assert r.status_code == 200


# ------------------------------ rotas de leitura -----------------------------
class TestRotasDeLeitura:
    def test_get_dados_so_para_admin(self, cli, tokens):
        cli.post("/dados", _app(), tokens["idoso"])
        server._flush_iteracao()
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

    def test_get_dados_guarda_no_maximo_max_em_memoria(self, cli, tokens, monkeypatch):
        monkeypatch.setattr(const_borda, "MAX_EM_MEMORIA", 2)
        for i in range(4):
            cli.post("/dados", _app(timestamp_envio=i), tokens["idoso"])
            server._flush_iteracao()
        assert len(cli.get("/dados", tokens["admin"]).get_json()) == 2
