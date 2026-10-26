"""Funcoes auxiliares da borda: verificar_versao, versao_atual_da_fonte, _ler_user_id."""
import pytest

from comum import const as const_comum
from borda import server


class TestVerificarVersao:
    ATUAL = const_comum.SCHEMA_VERSION_APP

    def test_ausente_vale_o_padrao(self):
        assert server.verificar_versao({}, self.ATUAL) == const_comum.SCHEMA_VERSION_PADRAO == 1

    @pytest.mark.parametrize("v", [1, 2, 3])
    def test_aceitas(self, v):
        assert server.verificar_versao({"schemaVersion": v}, 3) == v

    @pytest.mark.parametrize("v", [0, 4, -1, "3", 3.0, True, False, None, [3]])
    def test_rejeitadas(self, v):
        assert server.verificar_versao({"schemaVersion": v}, 3) is None

    def test_respeita_a_versao_atual_recebida(self):
        assert server.verificar_versao({"schemaVersion": 4}, 5) == 4
        assert server.verificar_versao({"schemaVersion": 4}, 3) is None

    def test_respeita_const_minima_alterada(self, monkeypatch):
        monkeypatch.setattr(const_comum, "SCHEMA_VERSION_MINIMA", 2)
        assert server.verificar_versao({"schemaVersion": 1}, 5) is None
        assert server.verificar_versao({"schemaVersion": 2}, 5) == 2


class TestVersaoAtualDaFonte:
    def test_cada_fonte_le_a_sua_constante(self, monkeypatch):
        monkeypatch.setattr(const_comum, "SCHEMA_VERSION_APP", 7)
        monkeypatch.setattr(const_comum, "SCHEMA_VERSION_WOKWI", 9)
        assert server.versao_atual_da_fonte("app") == 7
        assert server.versao_atual_da_fonte("wokwi") == 9

    def test_fonte_desconhecida_levanta(self):
        with pytest.raises(ValueError):
            server.versao_atual_da_fonte("outra")


class TestLerUserId:
    @pytest.mark.parametrize("v", [0, 1, 42])
    def test_inteiro_aceito(self, v):
        assert server._ler_user_id(v) == v

    @pytest.mark.parametrize("v", [True, False, 1.0, 1.9, "1", "01", " 1", None, [], {}, float("nan")])
    def test_o_resto_e_rejeitado(self, v):
        assert server._ler_user_id(v) is None
