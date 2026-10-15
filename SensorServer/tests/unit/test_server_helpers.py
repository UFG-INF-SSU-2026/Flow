"""Funcoes auxiliares de server.py: verificar_versao, _ler_user_id, _nome_seguro."""
import pytest

from types import SimpleNamespace

import const
import regra_queda
import server


class TestVerificarVersao:
    ATUAL = const.SCHEMA_VERSION_APP

    def test_ausente_vale_o_padrao(self):
        assert server.verificar_versao({}, self.ATUAL) == const.SCHEMA_VERSION_PADRAO == 1

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
        monkeypatch.setattr(const, "SCHEMA_VERSION_MINIMA", 2)
        assert server.verificar_versao({"schemaVersion": 1}, 5) is None
        assert server.verificar_versao({"schemaVersion": 2}, 5) == 2


class TestVersaoAtualDaFonte:
    def test_cada_fonte_le_a_sua_constante(self, monkeypatch):
        monkeypatch.setattr(const, "SCHEMA_VERSION_APP", 7)
        monkeypatch.setattr(const, "SCHEMA_VERSION_WOKWI", 9)
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


class TestNomeSeguro:
    def test_normal(self):
        assert server._nome_seguro("cel-1_a.b") == "cel-1_a.b"

    @pytest.mark.parametrize("entrada,esperado", [("../x", ".._x"), ("a/b\\c", "a_b_c"), ("ação", "a__o")])
    def test_troca_o_que_sai_do_conjunto(self, entrada, esperado):
        assert server._nome_seguro(entrada) == esperado

    def test_limite_de_tamanho(self):
        assert len(server._nome_seguro("a" * 500)) == const.MAX_DEVICE_ID_LEN

    def test_nao_sai_da_pasta(self):
        assert "/" not in server._nome_seguro("../../etc/passwd")


# ------------------------------- _despachar ---------------------------------
MSG = {"episodeId": "ep1", "messageId": "m1", "messageType": "queda.confirmacao_solicitada"}


def _canal(nome, resultado="ENTREGUE_AO_CANAL", falha=None, chamadas=None):
    def enviar(user_id, msg):
        if chamadas is not None:
            chamadas.append((nome, user_id, msg["messageId"]))
        if falha:
            raise falha
        return resultado
    return SimpleNamespace(CANAL=nome, enviar=enviar)


def _entregas(ler_confirmacoes):
    return [(l["canal"], l["resultado"]) for l in ler_confirmacoes(1) if l["tipo"] == "push.entrega"]


class TestDespachar:
    def test_registra_um_push_entrega_por_canal(self, monkeypatch, ler_confirmacoes):
        monkeypatch.setattr(server, "CANAIS", [_canal("a"), _canal("b", "SEM_CANAL_ATIVO")])
        server._despachar(1, MSG)
        assert _entregas(ler_confirmacoes) == [("a", "ENTREGUE_AO_CANAL"), ("b", "SEM_CANAL_ATIVO")]
        linha = ler_confirmacoes(1)[0]
        assert (linha["episodeId"], linha["messageId"], linha["messageType"]) == \
               ("ep1", "m1", "queda.confirmacao_solicitada")

    def test_falha_de_um_canal_nao_impede_o_outro(self, monkeypatch, ler_confirmacoes):
        chamadas = []
        monkeypatch.setattr(server, "CANAIS", [_canal("a", falha=RuntimeError("boom"), chamadas=chamadas),
                                               _canal("b", chamadas=chamadas)])
        server._despachar(1, MSG)                         # nao levanta
        assert [c[0] for c in chamadas] == ["a", "b"]
        assert _entregas(ler_confirmacoes) == [("a", "ERRO_NO_CANAL"), ("b", "ENTREGUE_AO_CANAL")]

    def test_falha_em_todos_os_canais(self, monkeypatch, ler_confirmacoes):
        monkeypatch.setattr(server, "CANAIS", [_canal("a", falha=OSError()), _canal("b", falha=ValueError())])
        server._despachar(1, MSG)
        assert _entregas(ler_confirmacoes) == [("a", "ERRO_NO_CANAL"), ("b", "ERRO_NO_CANAL")]

    def test_falha_ao_gravar_o_resultado_de_um_canal_nao_impede_o_outro(self, monkeypatch, ler_confirmacoes):
        chamadas = []
        monkeypatch.setattr(server, "CANAIS", [_canal("a", chamadas=chamadas), _canal("b", chamadas=chamadas)])
        original = regra_queda.registrar

        def registrar(uid, tipo, dados):
            if dados.get("canal") == "a":
                raise OSError("disco cheio")
            return original(uid, tipo, dados)

        monkeypatch.setattr(regra_queda, "registrar", registrar)
        server._despachar(1, MSG)
        assert [c[0] for c in chamadas] == ["a", "b"]
        assert _entregas(ler_confirmacoes) == [("b", "ENTREGUE_AO_CANAL")]
