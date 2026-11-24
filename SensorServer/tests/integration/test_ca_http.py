"""POST /auth/ca: entrega a CA do projeto a quem prova ter conta (sem token e sem certificado de cliente)."""

import pytest

from borda import const as const_borda
from comum import const as const_comum
from comum import tls

PEM = "-----BEGIN CERTIFICATE-----\nQUJD\n-----END CERTIFICATE-----\n"


@pytest.fixture
def ca_no_disco(tmp_path, monkeypatch):
    arq = tmp_path / "ca.crt"
    arq.write_text(PEM)
    monkeypatch.setenv("FLOW_TLS_CA", str(arq))
    return arq


def _pedir(cli, **campos):
    corpo = {"schemaVersion": const_comum.SCHEMA_VERSION_CA, "identificador": "idoso1", "segredo": "senha-idoso", **campos}
    return cli.post("/auth/ca", corpo)


class TestEntregaDaCa:
    def test_credenciais_certas_devolvem_a_ca_cifrada(self, cli, ca_no_disco):
        r = _pedir(cli)
        assert r.status_code == 200
        assert cli.corpo(r) == {"ca": PEM}
        assert "BEGIN CERTIFICATE" not in r.get_data(as_text=True)      # so o envelope cifrado trafega

    def test_nao_emite_token_nem_sessao(self, cli, ca_no_disco):
        assert set(cli.corpo(_pedir(cli))) == {"ca"}

    def test_senha_errada_e_conta_inexistente_dao_o_mesmo_401(self, cli, ca_no_disco):
        r1 = _pedir(cli, segredo="errada")
        r2 = _pedir(cli, identificador="ninguem")
        assert r1.status_code == r2.status_code == 401
        assert r1.get_json() == r2.get_json() == {"status": "erro", "codigo": "credenciais_invalidas"}

    def test_cinco_falhas_bloqueiam_ate_com_a_senha_certa(self, cli, ca_no_disco):
        for _ in range(const_borda.MAX_FALHAS):
            assert _pedir(cli, segredo="errada").status_code == 401
        r = _pedir(cli)
        assert r.status_code == 429 and r.get_json()["codigo"] == "bloqueado"

    def test_fonte_que_a_conta_nao_tem_da_401(self, cli, ca_no_disco):
        assert _pedir(cli, fonte="wokwi").status_code == 401

    @pytest.mark.parametrize("campos", [dict(identificador=""), dict(segredo=""), dict(identificador=5), dict(segredo=None)])
    def test_corpo_fora_do_contrato_da_400(self, cli, ca_no_disco, campos):
        assert _pedir(cli, **campos).status_code == 400

    @pytest.mark.parametrize("versao", [2, 0, "1", True])
    def test_schema_version_invalido_da_400(self, cli, ca_no_disco, versao):
        assert _pedir(cli, schemaVersion=versao).status_code == 400

    def test_sem_ca_no_servidor_da_503(self, cli, tmp_path, monkeypatch):
        monkeypatch.setenv("FLOW_TLS_CA", str(tmp_path / "nao-existe.crt"))
        r = _pedir(cli)
        assert r.status_code == 503 and r.get_json()["codigo"] == "ca_indisponivel"
