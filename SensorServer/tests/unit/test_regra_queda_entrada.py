"""regra_queda: normalizacao da leitura (extrair_dinamica) e deduplicacao do app (eh_duplicado_app)."""
import math

import pytest

import const
import regra_queda
from regra_queda import eh_duplicado_app, extrair_dinamica


def wokwi(value, vmax=None, **extra):
    d = {"eventType": "imobilidade.leitura", "value": value, **extra}
    if vmax is not None:
        d["valueMax"] = vmax
    return d


def app(m, mmax):
    return {"acelerometro": {"magnitude": m, "magnitude_max_janela": mmax}}


class TestExtrairWokwi:
    def test_sem_value_max_usa_value(self):
        assert extrair_dinamica("wokwi", wokwi(3.0)) == (3.0, 3.0)

    def test_com_value_max(self):
        assert extrair_dinamica("wokwi", wokwi(3.0, 25.0)) == (3.0, 25.0)

    def test_value_acima_de_value_max(self):
        assert extrair_dinamica("wokwi", wokwi(5.0, 4.0)) is None

    @pytest.mark.parametrize("v,vmax", [(-0.1, 1.0), (161.0, 161.0), (1.0, 161.0)])
    def test_fora_da_faixa_plausivel(self, v, vmax):
        assert extrair_dinamica("wokwi", wokwi(v, vmax)) is None

    def test_limites_da_faixa_valem(self):
        assert extrair_dinamica("wokwi", wokwi(0.0, 160.0)) == (0.0, 160.0)

    @pytest.mark.parametrize("v", [math.nan, "nan", "abc", None])
    def test_nan_e_nao_numerico(self, v):
        assert extrair_dinamica("wokwi", wokwi(v)) is None

    def test_event_type_errado(self):
        assert extrair_dinamica("wokwi", {**wokwi(3.0), "eventType": "imobilidade.decisao"}) is None

    def test_sem_value(self):
        assert extrair_dinamica("wokwi", {"eventType": "imobilidade.leitura"}) is None

    def test_fonte_desconhecida(self):
        assert extrair_dinamica("outra", wokwi(3.0)) is None


class TestExtrairApp:
    def test_normaliza_pela_gravidade(self):
        din, din_max = extrair_dinamica("app", app(12.81, 14.81))
        assert din == pytest.approx(3.0) and din_max == pytest.approx(5.0)

    def test_repouso_vira_zero(self):
        assert extrair_dinamica("app", app(9.81, 9.81)) == (pytest.approx(0.0), pytest.approx(0.0))

    def test_max_abaixo_da_gravidade_vira_zero(self):
        din, din_max = extrair_dinamica("app", app(8.81, 9.0))
        assert din == pytest.approx(1.0) and din_max == 0.0

    def test_sem_bloco(self):
        assert extrair_dinamica("app", {}) is None
        assert extrair_dinamica("app", {"acelerometro": {}}) is None

    @pytest.mark.parametrize("bloco", ["texto", [1, 2], 5])
    def test_bloco_que_nao_e_dict(self, bloco):
        assert extrair_dinamica("app", {"acelerometro": bloco}) is None

    def test_magnitude_acima_do_maximo_da_janela(self):
        assert extrair_dinamica("app", app(12.0, 10.0)) is None

    @pytest.mark.parametrize("m,mmax", [(-1.0, 9.0), (9.0, 161.0)])
    def test_fora_da_faixa_plausivel(self, m, mmax):
        assert extrair_dinamica("app", app(m, mmax)) is None

    def test_campo_faltando(self):
        assert extrair_dinamica("app", {"acelerometro": {"magnitude": 9.8}}) is None


def leitura(ts):
    return {"timestamp_envio": ts, **app(9.8, 9.9)}


class TestDeduplicacaoApp:
    def test_crescente_e_aceita(self):
        assert eh_duplicado_app(1, "d1", leitura(1000)) is False
        assert eh_duplicado_app(1, "d1", leitura(2000)) is False

    def test_igual_e_menor_sao_rejeitados(self):
        eh_duplicado_app(1, "d1", leitura(2000))
        assert eh_duplicado_app(1, "d1", leitura(2000)) is True
        assert eh_duplicado_app(1, "d1", leitura(1500)) is True

    def test_regressao_acima_da_tolerancia_vira_nova_referencia(self):
        eh_duplicado_app(1, "d1", leitura(1_000_000))
        recuado = 1_000_000 - 61_000                      # TOLERANCIA_RELOGIO_S = 60 s
        assert eh_duplicado_app(1, "d1", leitura(recuado)) is False
        assert eh_duplicado_app(1, "d1", leitura(recuado)) is True   # agora e a referencia
        assert eh_duplicado_app(1, "d1", leitura(recuado + 1)) is False

    def test_regressao_dentro_da_tolerancia_e_duplicata(self):
        eh_duplicado_app(1, "d1", leitura(1_000_000))
        assert eh_duplicado_app(1, "d1", leitura(1_000_000 - 60_000)) is True

    @pytest.mark.parametrize("ts", [None, True, "123", math.nan, math.inf])
    def test_timestamp_ausente_ou_invalido_nao_e_duplicata(self, ts):
        assert eh_duplicado_app(1, "d1", leitura(ts)) is False
        assert eh_duplicado_app(1, "d1", leitura(ts)) is False
        assert regra_queda._ultimo_ts_app == {}           # nao toca no estado

    def test_sem_timestamp_envio(self):
        assert eh_duplicado_app(1, "d1", app(9.8, 9.9)) is False

    def test_sem_acelerometro_aproveitavel(self):
        assert eh_duplicado_app(1, "d1", {"timestamp_envio": 1000}) is False
        assert regra_queda._ultimo_ts_app == {}

    def test_dispositivos_e_usuarios_independentes(self):
        eh_duplicado_app(1, "d1", leitura(2000))
        assert eh_duplicado_app(1, "d2", leitura(2000)) is False
        assert eh_duplicado_app(2, "d1", leitura(2000)) is False

    def test_limite_de_referencias_descarta_a_mais_antiga(self, monkeypatch):
        monkeypatch.setattr(const, "MAX_REFERENCIAS_DEDUP", 3)
        for i in range(4):
            eh_duplicado_app(1, f"d{i}", leitura(1000))
        assert set(regra_queda._ultimo_ts_app) == {(1, "app", "d1"), (1, "app", "d2"), (1, "app", "d3")}
