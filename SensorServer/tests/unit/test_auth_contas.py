"""auth.criar_conta: validacoes (P-05)."""
import pytest

import auth
import const


def _contas() -> dict:
    return auth._ler_json(const.CONTAS_PATH, {})


class TestValidas:
    def test_idoso(self):
        auth.criar_conta("i", "idoso", [1], ["app"], "s")
        c = _contas()["i"]
        assert (c["papel"], c["user_ids"], c["fontes"]) == ("idoso", [1], ["app"])
        assert auth._verificar_senha("s", c["hash"])

    def test_dispositivo(self):
        auth.criar_conta("d", "dispositivo", [1], ["wokwi"], "s")
        assert _contas()["d"]["fontes"] == ["wokwi"]

    def test_admin_sem_user_id(self):
        auth.criar_conta("a", "admin", [], [], "s")
        assert _contas()["a"]["user_ids"] == []

    def test_cuidador_reservado_e_criavel(self):
        auth.criar_conta("c", "cuidador", [1, 2], [], "s")
        assert _contas()["c"]["user_ids"] == [1, 2]


class TestInvalidas:
    def test_duplicada(self):
        auth.criar_conta("i", "idoso", [1], ["app"], "s")
        with pytest.raises(ValueError, match="ja existe"):
            auth.criar_conta("i", "idoso", [1], ["app"], "s")

    def test_papel_invalido(self):
        with pytest.raises(ValueError, match="papel"):
            auth.criar_conta("x", "root", [1], ["app"], "s")

    def test_fonte_invalida(self):
        with pytest.raises(ValueError, match="fontes"):
            auth.criar_conta("x", "idoso", [1], ["xyz"], "s")

    @pytest.mark.parametrize("papel,fontes", [("idoso", ["app"]), ("dispositivo", ["wokwi"])])
    @pytest.mark.parametrize("user_ids", [[], [1, 2]])
    def test_idoso_e_dispositivo_exigem_exatamente_um_user_id(self, papel, fontes, user_ids):
        with pytest.raises(ValueError, match="exatamente um"):
            auth.criar_conta("x", papel, user_ids, fontes, "s")

    def test_idoso_com_fonte_wokwi(self):
        with pytest.raises(ValueError, match="fontes"):
            auth.criar_conta("x", "idoso", [1], ["wokwi"], "s")

    def test_dispositivo_com_fonte_app(self):
        with pytest.raises(ValueError, match="fontes"):
            auth.criar_conta("x", "dispositivo", [1], ["app"], "s")

    @pytest.mark.parametrize("papel", ["idoso", "dispositivo"])
    def test_fontes_vazias(self, papel):
        with pytest.raises(ValueError, match="fontes"):
            auth.criar_conta("x", papel, [1], [], "s")

    def test_idoso_com_as_duas_fontes(self):
        with pytest.raises(ValueError, match="fontes"):
            auth.criar_conta("x", "idoso", [1], ["app", "wokwi"], "s")

    def test_admin_com_user_ids(self):
        with pytest.raises(ValueError, match="admin"):
            auth.criar_conta("x", "admin", [1], [], "s")

    @pytest.mark.parametrize("uid", [-1, True, 1.5, "1"])
    def test_user_id_invalido(self, uid):
        with pytest.raises(ValueError, match="user_id"):
            auth.criar_conta("x", "idoso", [uid], ["app"], "s")

    def test_conta_invalida_nao_e_gravada(self):
        with pytest.raises(ValueError):
            auth.criar_conta("x", "idoso", [1], ["wokwi"], "s")
        assert "x" not in _contas()
