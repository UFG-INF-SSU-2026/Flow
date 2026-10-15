"""auth.py: senhas, login, bloqueio, refresh rotativo, tokens e segredo."""
import time

import jwt
import pytest

import auth
import const
from _apoio import SEGREDO


def _errar(n: int, ident: str = "idoso1") -> None:
    for _ in range(n):
        with pytest.raises(auth.CredenciaisInvalidas):
            auth.login(ident, "senha-errada", "app", "cel1")


def _login_ok() -> dict:
    return auth.login("idoso1", "senha-idoso", "app", "cel1")


# ------------------------------- senhas -------------------------------------
class TestSenha:
    def test_hash_tem_formato_e_valida_a_senha_certa(self):
        h = auth.hash_senha("abc")
        assert h.startswith("scrypt$") and h.count("$") == 2
        assert auth._verificar_senha("abc", h) is True

    def test_senha_errada(self):
        assert auth._verificar_senha("xyz", auth.hash_senha("abc")) is False

    def test_sal_aleatorio(self):
        assert auth.hash_senha("abc") != auth.hash_senha("abc")

    @pytest.mark.parametrize("armazenado", ["", "lixo", "a$b", "scrypt$zz$zz", "scrypt$00$"])
    def test_formato_invalido_devolve_false_sem_levantar(self, armazenado):
        assert auth._verificar_senha("abc", armazenado) is False


# -------------------------------- login -------------------------------------
class TestLogin:
    def test_ok_devolve_as_cinco_chaves(self, contas):
        t = _login_ok()
        assert set(t) == {"access_token", "refresh_token", "expires_in", "papel", "user_ids"}
        assert t["papel"] == "idoso" and t["user_ids"] == [1]
        assert t["expires_in"] == const.ACCESS_TTL_S
        assert t["access_token"] and t["refresh_token"]

    def test_senha_errada_e_conta_inexistente_levantam_o_mesmo_erro(self, contas):
        with pytest.raises(auth.AuthError) as e1:
            auth.login("idoso1", "errada", "app", "cel1")
        with pytest.raises(auth.AuthError) as e2:
            auth.login("ninguem", "qualquer", "app", "cel1")
        assert type(e1.value) is type(e2.value) is auth.CredenciaisInvalidas
        assert e1.value.codigo == e2.value.codigo == "credenciais_invalidas"

    def test_cinco_falhas_bloqueiam_ate_com_a_senha_certa(self, contas, relogio_auth):
        _errar(const.MAX_FALHAS)
        with pytest.raises(auth.Bloqueado):
            _login_ok()

    def test_bloqueio_expira(self, contas, relogio_auth):
        _errar(const.MAX_FALHAS)
        relogio_auth.avancar(const.BLOQUEIO_S - 1)
        with pytest.raises(auth.Bloqueado):
            _login_ok()
        relogio_auth.avancar(2)
        assert _login_ok()["papel"] == "idoso"

    def test_falhas_fora_da_janela_nao_contam(self, contas, relogio_auth):
        _errar(const.MAX_FALHAS - 1)
        relogio_auth.avancar(const.JANELA_FALHAS_S + 1)
        _errar(1)
        assert _login_ok()["papel"] == "idoso"

    def test_login_ok_zera_as_falhas(self, contas, relogio_auth):
        _errar(const.MAX_FALHAS - 1)
        _login_ok()
        _errar(const.MAX_FALHAS - 1)     # com as falhas antigas somadas, teria bloqueado
        assert _login_ok()["papel"] == "idoso"

    def test_bloqueio_e_por_identificador(self, contas, relogio_auth):
        _errar(const.MAX_FALHAS)
        assert auth.login("bridge1", "senha-bridge", "wokwi", "b")["papel"] == "dispositivo"

    def test_fonte_nao_permitida_falha(self, contas):
        for fonte in ("wokwi", None, "xyz"):
            with pytest.raises(auth.CredenciaisInvalidas):
                auth.login("idoso1", "senha-idoso", fonte, "cel1")

    def test_admin_so_entra_sem_fonte(self, contas):
        assert auth.login("admin1", "senha-admin", None, None)["papel"] == "admin"
        with pytest.raises(auth.CredenciaisInvalidas):
            auth.login("admin1", "senha-admin", "app", None)

    def test_device_id_invalido_levanta_value_error(self, contas):
        with pytest.raises(ValueError):
            auth.login("idoso1", "senha-idoso", "app", "x" * (const.MAX_DEVICE_ID_LEN + 1))


class TestDeviceId:
    @pytest.mark.parametrize("valor,esperado", [
        (None, True), ("a" * 64, True), ("a" * 65, False), (123, False), (["a"], False), (b"x", False),
    ])
    def test_device_id_valido(self, valor, esperado):
        assert auth.device_id_valido(valor) is esperado


# -------------------------------- refresh -----------------------------------
class TestRefresh:
    def test_devolve_novo_par_e_invalida_o_anterior(self, contas):
        antigo = _login_ok()
        novo = auth.refresh(antigo["refresh_token"])
        assert set(novo) == set(antigo)
        assert novo["refresh_token"] != antigo["refresh_token"]
        assert novo["access_token"] != antigo["access_token"]
        with pytest.raises(auth.TokenInvalido):
            auth.refresh(antigo["refresh_token"])          # reuso
        assert auth.refresh(novo["refresh_token"])["papel"] == "idoso"   # o novo vale

    def test_desconhecido_falha(self, contas):
        with pytest.raises(auth.TokenInvalido):
            auth.refresh("nao-existe")

    def test_expirado_falha(self, contas, relogio_auth, monkeypatch):
        monkeypatch.setattr(const, "REFRESH_TTL_S", 10)
        t = _login_ok()
        relogio_auth.avancar(11)
        with pytest.raises(auth.TokenInvalido):
            auth.refresh(t["refresh_token"])

    def test_dentro_da_validade_funciona(self, contas, relogio_auth, monkeypatch):
        monkeypatch.setattr(const, "REFRESH_TTL_S", 10)
        t = _login_ok()
        relogio_auth.avancar(9)
        assert auth.refresh(t["refresh_token"])["papel"] == "idoso"

    def test_conta_removida_falha(self, contas):
        t = _login_ok()
        auth._gravar_json(const.CONTAS_PATH, {})
        with pytest.raises(auth.TokenInvalido):
            auth.refresh(t["refresh_token"])

    def test_preserva_fonte_e_device_id(self, contas):
        novo = auth.refresh(_login_ok()["refresh_token"])
        c = jwt.decode(novo["access_token"], SEGREDO, algorithms=["HS256"])
        assert c["fonte"] == "app" and c["device_id"] == "cel1"


# --------------------------------- tokens -----------------------------------
class TestToken:
    def test_claims_do_access_token(self, contas):
        c = jwt.decode(_login_ok()["access_token"], SEGREDO, algorithms=["HS256"])
        assert c["sub"] == "idoso1" and c["papel"] == "idoso" and c["user_ids"] == [1]
        assert c["fonte"] == "app" and c["device_id"] == "cel1"
        assert c["exp"] - c["iat"] == const.ACCESS_TTL_S
        assert isinstance(c["jti"], str) and len(c["jti"]) == 32

    def test_verificar_token_devolve_principal(self, contas):
        p = auth.verificar_token(_login_ok()["access_token"])
        assert (p.conta_id, p.papel, p.user_ids, p.fonte, p.device_id) == ("idoso1", "idoso", (1,), "app", "cel1")
        assert isinstance(p.exp, float) and p.exp > time.time()

    def test_expirado(self, contas, monkeypatch):
        monkeypatch.setattr(const, "ACCESS_TTL_S", -10)
        with pytest.raises(auth.TokenInvalido):
            auth.verificar_token(_login_ok()["access_token"])

    def test_assinatura_errada(self):
        t = jwt.encode({"sub": "a", "exp": int(time.time()) + 60}, "x" * 48, algorithm="HS256")
        with pytest.raises(auth.TokenInvalido):
            auth.verificar_token(t)

    def test_sem_exp(self):
        with pytest.raises(auth.TokenInvalido):
            auth.verificar_token(jwt.encode({"sub": "a"}, SEGREDO, algorithm="HS256"))

    def test_sem_sub(self):
        with pytest.raises(auth.TokenInvalido):
            auth.verificar_token(jwt.encode({"exp": int(time.time()) + 60}, SEGREDO, algorithm="HS256"))

    def test_lixo(self):
        with pytest.raises(auth.TokenInvalido):
            auth.verificar_token("nao-e-um-jwt")


# --------------------------------- segredo ----------------------------------
class TestSegredo:
    def test_ausente_levanta(self, monkeypatch):
        monkeypatch.delenv("FLOW_JWT_SECRET", raising=False)
        with pytest.raises(RuntimeError):
            auth.exigir_segredo()

    def test_menos_de_32_bytes_levanta(self, monkeypatch):
        monkeypatch.setenv("FLOW_JWT_SECRET", "x" * 31)
        with pytest.raises(RuntimeError):
            auth.exigir_segredo()

    def test_32_bytes_basta(self, monkeypatch):
        monkeypatch.setenv("FLOW_JWT_SECRET", "x" * 32)
        auth.exigir_segredo()

    def test_conta_bytes_e_nao_caracteres(self, monkeypatch):
        monkeypatch.setenv("FLOW_JWT_SECRET", "ç" * 16)    # 16 caracteres, 32 bytes em UTF-8
        auth.exigir_segredo()
