"""authz.py: tabela de politica, escopo, decorador exige e auditoria."""
import json
from datetime import datetime

import pytest
from flask import Flask, g, jsonify

import auth
import authz
import const

PERMISSOES = ["dados:enviar", "eventos:enviar", "push:receber", "dados:ler", "estado:ler"]
ESPERADO = {
    "idoso": {"dados:enviar", "eventos:enviar", "push:receber"},
    "dispositivo": {"dados:enviar", "eventos:enviar", "push:receber"},
    "cuidador": set(),
    "admin": {"dados:ler", "estado:ler"},
}


class TestPolitica:
    def test_cobre_exatamente_os_papeis_validos(self):
        assert set(authz.POLITICA) == const.PAPEIS_VALIDOS == set(ESPERADO)

    @pytest.mark.parametrize("papel", sorted(ESPERADO))
    @pytest.mark.parametrize("permissao", PERMISSOES)
    def test_papel_x_permissao(self, papel, permissao):
        assert (permissao in authz.POLITICA[papel]) is (permissao in ESPERADO[papel])

    def test_cuidador_nao_tem_nada(self):
        assert authz.POLITICA["cuidador"] == set()

    def test_fonte_do_papel(self):
        assert authz.FONTE_DO_PAPEL == {"idoso": "app", "dispositivo": "wokwi"}


class TestEscopo:
    P = auth.Principal("idoso1", "idoso", (1,), "app", "cel1", 9e9)

    def test_valido(self):
        authz.exigir_escopo(self.P, 1, "app")

    def test_user_id_fora(self):
        with pytest.raises(authz.ForaDeEscopo):
            authz.exigir_escopo(self.P, 2, "app")

    def test_source_diferente_do_token(self):
        with pytest.raises(authz.ForaDeEscopo):
            authz.exigir_escopo(self.P, 1, "wokwi")


@pytest.fixture
def mini(contas):
    """App Flask minimo so com rotas protegidas por exige, mais os tokens de cada conta."""
    app = Flask("mini")

    @app.route("/enviar")
    @authz.exige("dados:enviar")
    def enviar():
        return jsonify(conta=g.principal.conta_id)

    @app.route("/so-wokwi")
    @authz.exige("dados:enviar", fonte="wokwi")
    def so_wokwi():
        return jsonify(ok=True)

    @app.route("/ler")
    @authz.exige("dados:ler")
    def ler():
        return jsonify(ok=True)

    tok = {
        "idoso": auth.login("idoso1", "senha-idoso", "app", "cel1")["access_token"],
        "bridge": auth.login("bridge1", "senha-bridge", "wokwi", "b1")["access_token"],
    }
    return app.test_client(), tok


def _bearer(t):
    return {"Authorization": f"Bearer {t}"}


class TestExige:
    def test_sem_cabecalho(self, mini):
        c, _ = mini
        r = c.get("/enviar")
        assert r.status_code == 401
        assert r.headers["WWW-Authenticate"] == "Bearer"
        assert r.get_json()["codigo"] == "token_ausente"

    def test_cabecalho_que_nao_e_bearer(self, mini):
        c, _ = mini
        r = c.get("/enviar", headers={"Authorization": "Basic abc"})
        assert r.status_code == 401 and r.get_json()["codigo"] == "token_ausente"

    def test_token_invalido(self, mini):
        c, _ = mini
        r = c.get("/enviar", headers=_bearer("abc.def.ghi"))
        assert r.status_code == 401 and r.get_json()["codigo"] == "token_invalido"

    def test_sem_permissao(self, mini):
        c, tok = mini
        r = c.get("/ler", headers=_bearer(tok["idoso"]))
        assert r.status_code == 403 and r.get_json()["codigo"] == "sem_permissao"
        assert "WWW-Authenticate" not in r.headers

    def test_fonte_do_papel_errada(self, mini):
        c, _ = mini
        auth.criar_conta("idoso-x", "idoso", [1], ["app", "wokwi"], "senha-x")   # P-05: criar_conta nao barra
        t = auth.login("idoso-x", "senha-x", "wokwi", "d")["access_token"]
        r = c.get("/enviar", headers=_bearer(t))
        assert r.status_code == 403 and r.get_json()["codigo"] == "fonte_invalida"

    def test_fonte_da_rota_errada(self, mini):
        c, tok = mini
        r = c.get("/so-wokwi", headers=_bearer(tok["idoso"]))
        assert r.status_code == 403 and r.get_json()["codigo"] == "fonte_invalida"

    def test_caso_valido_expoe_o_principal(self, mini):
        c, tok = mini
        r = c.get("/enviar", headers=_bearer(tok["idoso"]))
        assert r.status_code == 200 and r.get_json()["conta"] == "idoso1"
        assert c.get("/so-wokwi", headers=_bearer(tok["bridge"])).status_code == 200


class TestAuditoria:
    @staticmethod
    def _auditar(motivo: str) -> None:
        app = Flask("aud")
        with app.test_request_context("/rota-x", environ_base={"REMOTE_ADDR": "10.1.2.3"}):
            authz.auditar("/rota-x", 403, motivo, "idoso1")

    @staticmethod
    def _ler(nome: str) -> list[dict]:
        with open(f"{const.DATA_DIR}/{nome}", encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]

    def test_linha_com_os_campos(self):
        self._auditar("sem_permissao")
        (linha,) = self._ler("auditoria.jsonl")
        assert set(linha) == {"timestamp", "rota", "status", "motivo", "conta", "ip"}
        assert (linha["rota"], linha["status"], linha["motivo"], linha["conta"], linha["ip"]) == \
               ("/rota-x", 403, "sem_permissao", "idoso1", "10.1.2.3")
        datetime.fromisoformat(linha["timestamp"])

    def test_rotacao_sobrescreve_o_arquivo_anterior(self, monkeypatch):
        monkeypatch.setattr(const, "AUDITORIA_MAX_BYTES", 100)   # uma linha ja passa disso
        self._auditar("a")
        self._auditar("b")      # a vira auditoria.1.jsonl, b abre o novo
        assert [l["motivo"] for l in self._ler("auditoria.1.jsonl")] == ["a"]
        assert [l["motivo"] for l in self._ler("auditoria.jsonl")] == ["b"]
        self._auditar("c")      # b sobrescreve o .1, c abre o novo; a some
        assert [l["motivo"] for l in self._ler("auditoria.1.jsonl")] == ["b"]
        assert [l["motivo"] for l in self._ler("auditoria.jsonl")] == ["c"]
