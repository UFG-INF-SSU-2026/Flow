"""authz.py: tabela de politica, escopo, decorador exige e registro da negacao."""

import pytest
from flask import Flask, g, jsonify

from borda import auth
from borda import authz
from borda import const as const_borda
PERMISSOES = ["dados:enviar", "eventos:enviar", "push:receber", "dados:ler", "estado:ler"]
ESPERADO = {
    "idoso": {"dados:enviar", "eventos:enviar", "push:receber"},
    "dispositivo": {"dados:enviar", "eventos:enviar", "push:receber"},
    "cuidador": set(),
    "admin": {"dados:ler", "estado:ler"},
}


class TestPolitica:
    def test_cobre_exatamente_os_papeis_validos(self):
        assert set(authz.POLITICA) == const_borda.PAPEIS_VALIDOS == set(ESPERADO)

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


class TestDispositivo:
    P = auth.Principal("idoso1", "idoso", (1,), "app", "cel1", 9e9)

    def test_igual_ao_do_token(self):
        authz.exigir_dispositivo(self.P, {"device_id": "cel1"})
        authz.exigir_dispositivo(self.P, {"deviceId": "cel1"})
        authz.exigir_dispositivo(self.P, {"device_id": "cel1", "deviceId": "cel1"})

    def test_nao_declarado_nao_e_conferido(self):
        for payload in ({}, {"device_id": None}, {"device_id": ""}, {"deviceId": None, "device_id": ""}):
            authz.exigir_dispositivo(self.P, payload)

    @pytest.mark.parametrize("payload", [
        {"device_id": "outro"}, {"deviceId": "outro"}, {"device_id": "cel1", "deviceId": "outro"},
        {"device_id": 5}, {"device_id": ["cel1"]}, {"device_id": "CEL1"},
    ])
    def test_diferente_do_token_levanta(self, payload):
        with pytest.raises(authz.ForaDeEscopo):
            authz.exigir_dispositivo(self.P, payload)

    def test_token_sem_device_id_nao_aceita_device_declarado(self):
        sem_device = auth.Principal("idoso1", "idoso", (1,), "app", None, 9e9)
        authz.exigir_dispositivo(sem_device, {})
        with pytest.raises(authz.ForaDeEscopo):
            authz.exigir_dispositivo(sem_device, {"device_id": "cel1"})


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
        # criar_conta barra esta combinacao; grava direto para simular uma conta mal configurada
        cs = auth._ler_json(const_borda.CONTAS_PATH, {})
        cs["idoso-x"] = {"id": "idoso-x", "papel": "idoso", "user_ids": [1], "fontes": ["app", "wokwi"],
                         "hash": auth.hash_senha("senha-x")}
        auth._gravar_json(const_borda.CONTAS_PATH, cs)
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


class TestRegistroDaNegacao:
    def test_linha_de_log_com_rota_motivo_conta_e_ip_sem_conteudo(self, capsys):
        app = Flask("aud")
        with app.test_request_context("/rota-x", environ_base={"REMOTE_ADDR": "10.1.2.3"}):
            authz.auditar("/rota-x", 403, "sem_permissao", "idoso1")
        saida = capsys.readouterr().out
        assert "negado 403 sem_permissao rota=/rota-x conta=idoso1 ip=10.1.2.3" in saida
