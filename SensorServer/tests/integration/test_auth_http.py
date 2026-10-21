"""POST /auth/login e /auth/refresh pelo test_client, incluindo a regra de auditoria do login falho."""
import json

import pytest

from comum import const as const_comum
from borda import const as const_borda
def _login(cli, **campos):
    corpo = {"schemaVersion": const_comum.SCHEMA_VERSION_AUTH, "identificador": "idoso1", "segredo": "senha-idoso", "fonte": "app",
             "device_id": "cel1", **campos}
    return cli.post("/auth/login", corpo)


def _auditoria() -> list[dict]:
    with open(f"{const_comum.DATA_DIR}/auditoria.jsonl", encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


class TestLogin:
    def test_ok_decifra_com_cinco_campos(self, cli):
        r = _login(cli)
        assert r.status_code == 200
        t = cli.corpo(r)
        assert set(t) == {"access_token", "refresh_token", "expires_in", "papel", "user_ids"}
        assert t["papel"] == "idoso" and t["user_ids"] == [1]

    def test_senha_errada_e_conta_inexistente_dao_o_mesmo_401(self, cli):
        r1 = _login(cli, segredo="errada")
        r2 = _login(cli, identificador="ninguem")
        assert r1.status_code == r2.status_code == 401
        assert r1.get_json() == r2.get_json() == {"status": "erro", "codigo": "credenciais_invalidas"}
        assert r1.headers["WWW-Authenticate"] == r2.headers["WWW-Authenticate"] == "Bearer"

    def test_cinco_falhas_dao_429(self, cli):
        for _ in range(const_borda.MAX_FALHAS):
            assert _login(cli, segredo="errada").status_code == 401
        r = _login(cli)                       # ate com a senha certa
        assert r.status_code == 429 and r.get_json()["codigo"] == "bloqueado"

    @pytest.mark.parametrize("versao", [4, 0, "3", True, 3.0])
    def test_schema_version_invalido_da_400(self, cli, versao):
        assert _login(cli, schemaVersion=versao).status_code == 400

    def test_schema_version_ausente_e_aceito(self, cli):
        corpo = {"identificador": "idoso1", "segredo": "senha-idoso", "fonte": "app", "device_id": "cel1"}
        assert cli.post("/auth/login", corpo).status_code == 200

    def test_device_id_longo_da_400(self, cli):
        assert _login(cli, device_id="x" * (const_comum.MAX_DEVICE_ID_LEN + 1)).status_code == 400

    def test_envelope_malformado_nao_decifravel_e_corpo_nao_objeto_dao_400(self, cli):
        assert cli.post_cru("/auth/login", {"x": 1}).status_code == 400
        assert cli.post_cru("/auth/login", [1, 2]).status_code == 400
        assert cli.post_cru("/auth/login", {"nonce": "AAAA", "ciphertext": "AAAA"}).status_code == 400
        assert cli.post("/auth/login", [1, 2]).status_code == 400
        assert cli.post("/auth/login", "texto").status_code == 400


class TestRefresh:
    def test_ok_e_reuso_da_401(self, cli):
        t = cli.corpo(_login(cli))
        r = cli.post("/auth/refresh", {"refresh_token": t["refresh_token"]})
        assert r.status_code == 200
        novo = cli.corpo(r)
        assert novo["refresh_token"] != t["refresh_token"]
        assert cli.post("/auth/refresh", {"refresh_token": t["refresh_token"]}).status_code == 401
        assert cli.post("/auth/refresh", {"refresh_token": novo["refresh_token"]}).status_code == 200

    def test_schema_version_invalido_da_400(self, cli):
        assert cli.post("/auth/refresh", {"schemaVersion": 4, "refresh_token": "x"}).status_code == 400

    def test_envelope_ruim_da_400(self, cli):
        assert cli.post_cru("/auth/refresh", {"x": 1}).status_code == 400


class TestAuditoriaDoLogin:
    def test_falhas_ficam_em_auditoria_jsonl(self, cli):
        _login(cli, segredo="errada")
        (linha,) = _auditoria()
        assert (linha["rota"], linha["status"], linha["motivo"]) == ("/auth/login", 401, "credenciais_invalidas")

    def test_conta_existente_e_gravada(self, cli):
        _login(cli, segredo="errada")
        assert _auditoria()[0]["conta"] == "idoso1"

    def test_conta_inexistente_nao_e_gravada(self, cli):
        _login(cli, identificador="digitado-pelo-atacante")
        (linha,) = _auditoria()
        assert linha["conta"] is None
        with open(f"{const_comum.DATA_DIR}/auditoria.jsonl", encoding="utf-8") as f:
            assert "digitado-pelo-atacante" not in f.read()

    def test_fonte_errada_de_conta_existente_grava_a_conta(self, cli):
        _login(cli, fonte="wokwi")
        assert _auditoria()[0]["conta"] == "idoso1"

    def test_429_de_conta_existente_grava_a_conta(self, cli):
        for _ in range(const_borda.MAX_FALHAS):
            _login(cli, segredo="errada")
        _login(cli)
        linhas = _auditoria()
        assert [l["status"] for l in linhas] == [401] * const_borda.MAX_FALHAS + [429]
        assert linhas[-1]["conta"] == "idoso1" and linhas[-1]["motivo"] == "bloqueado"

    def test_429_de_conta_inexistente_nao_grava_a_conta(self, cli):
        for _ in range(const_borda.MAX_FALHAS):
            _login(cli, identificador="fantasma", segredo="x")
        _login(cli, identificador="fantasma", segredo="x")
        linhas = _auditoria()
        assert linhas[-1]["status"] == 429
        assert all(l["conta"] is None for l in linhas)
