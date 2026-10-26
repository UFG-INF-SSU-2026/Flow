"""Autorizacao pelas rotas HTTP (test_client): matriz rota x papel, escopo e auditoria."""
from borda import const as const_borda

import pytest

# (metodo, rota) -> status esperado por papel. POST usa corpo `{}`: autorizado chega ao estagio 2 e da 400.
MATRIZ = {
    ("POST", "/dados"):        {"idoso": 400, "dispositivo": 400, "admin": 403, "sem_token": 401, "invalido": 401},
    ("POST", "/eventos"):      {"idoso": 400, "dispositivo": 400, "admin": 403, "sem_token": 401, "invalido": 401},
    ("GET", "/push/app"):      {"idoso": 200, "dispositivo": 403, "admin": 403, "sem_token": 401, "invalido": 401},
    ("GET", "/push/wokwi"):    {"idoso": 403, "dispositivo": 200, "admin": 403, "sem_token": 401, "invalido": 401},
    ("GET", "/dados"):         {"idoso": 403, "dispositivo": 403, "admin": 200, "sem_token": 401, "invalido": 401},
    ("GET", "/estado/nada"):   {"idoso": 403, "dispositivo": 403, "admin": 404, "sem_token": 401, "invalido": 401},
    ("GET", "/"):              {"idoso": 200, "dispositivo": 200, "admin": 200, "sem_token": 200, "invalido": 200},
    ("GET", "/health"):        {"idoso": 200, "dispositivo": 200, "admin": 200, "sem_token": 200, "invalido": 200},
}
CASOS = [(m, r, papel, st) for (m, r), por_papel in MATRIZ.items() for papel, st in por_papel.items()]


@pytest.mark.parametrize("metodo,rota,papel,esperado", CASOS)
def test_matriz_rota_x_papel(cli, tokens, metodo, rota, papel, esperado):
    token = tokens[papel]
    r = cli.post(rota, {}, token) if metodo == "POST" else cli.get(rota, token)
    try:
        assert r.status_code == esperado
        if esperado == 401:
            assert r.headers["WWW-Authenticate"] == "Bearer"
    finally:
        r.close()    # fecha o stream SSE dos casos 200 em /push/*


def test_token_expirado_da_401(cli, contas, monkeypatch):
    monkeypatch.setattr(const_borda, "ACCESS_TTL_S", -10)
    token = cli.login("idoso1", "senha-idoso", "app", "cel1")["access_token"]
    r = cli.post("/dados", {"source": "app", "user_id": 1}, token)
    assert r.status_code == 401 and r.get_json()["codigo"] == "token_invalido"


@pytest.mark.parametrize("rota", ["/dados", "/eventos"])
class TestEscopo:
    def test_source_ausente_ou_invalida_da_400(self, cli, tokens, rota):
        assert cli.post(rota, {"user_id": 1}, tokens["idoso"]).status_code == 400
        assert cli.post(rota, {"source": "xyz", "user_id": 1}, tokens["idoso"]).status_code == 400

    def test_source_diferente_da_do_token_da_403(self, cli, tokens, rota):
        r = cli.post(rota, {"source": "wokwi", "user_id": 1}, tokens["idoso"])
        assert r.status_code == 403 and r.get_json()["codigo"] == "fora_de_escopo"

    def test_user_id_de_outra_pessoa_da_403(self, cli, tokens, rota):
        r = cli.post(rota, {"source": "app", "user_id": 2}, tokens["idoso"])
        assert r.status_code == 403 and r.get_json()["codigo"] == "fora_de_escopo"

    def test_400_vem_antes_do_403(self, cli, tokens, rota):
        # source diferente do token (daria 403), mas user_id invalido: a validacao do formato vem primeiro
        assert cli.post(rota, {"source": "wokwi", "user_id": True}, tokens["idoso"]).status_code == 400
        assert cli.post(rota, {"user_id": 2}, tokens["idoso"]).status_code == 400


def test_negacao_vai_para_o_log_sem_o_conteudo(cli, tokens, capsys):
    marcador = "SEGREDO-XYZ-123"
    assert cli.post("/dados", {"source": "app", "user_id": 2, "nota": marcador}, tokens["idoso"]).status_code == 403
    assert cli.get("/dados").status_code == 401
    saida = capsys.readouterr().out
    assert "negado 403 fora_de_escopo rota=/dados conta=idoso1" in saida
    assert "negado 401 token_ausente rota=/dados conta=-" in saida
    assert marcador not in saida
