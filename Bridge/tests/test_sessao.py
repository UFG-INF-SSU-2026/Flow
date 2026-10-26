"""Sessao de um dispositivo: login repassado, renovacao e envio. O servidor e trocado por um dublê de requests.post."""
import pytest
import requests

import sessao as sessao_mod
from apoio import USER_ID
from sessao import ErroServidor, Relogin, ServidorIndisponivel, Sessao


class RespostaFalsa:
    def __init__(self, status=200, corpo=None, texto=""):
        self.status_code, self._corpo, self.text = status, corpo, texto

    def json(self):
        return self._corpo


def tokens(access, refresh):
    """Resposta de /auth/login ou /auth/refresh: o mesmo envelope cifrado do servidor."""
    return RespostaFalsa(200, sessao_mod.cifrar({"access_token": access, "refresh_token": refresh, "user_ids": [USER_ID]}))


@pytest.fixture
def servidor_falso(monkeypatch, cfg):
    """Troca requests.post: `respostas` e uma fila de RespostaFalsa (ou excecao); `chamadas` guarda (rota, corpo claro)."""
    class Servidor:
        respostas: list = []
        chamadas: list = []

    def post(url, **kw):
        Servidor.chamadas.append((url.removeprefix(cfg.servidor), sessao_mod.decifrar(kw["json"]), kw.get("verify")))
        item = Servidor.respostas.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    Servidor.respostas, Servidor.chamadas = [], []
    monkeypatch.setattr(sessao_mod.requests, "post", post)
    return Servidor


@pytest.fixture
def sessao(cfg):
    s = Sessao("esp32-teste", cfg)
    s.access, s.refresh_token, s.user_id = "A1", "R1", USER_ID
    return s


def test_login_repassa_credenciais_fonte_e_device(servidor_falso, cfg):
    servidor_falso.respostas = [tokens("A9", "R9")]
    s = Sessao("esp32-teste", cfg)
    s.login("bridge-teste", "segredo-teste")
    (rota, corpo, verify), = servidor_falso.chamadas
    assert rota == "/auth/login"
    assert corpo == {"schemaVersion": 3, "identificador": "bridge-teste", "segredo": "segredo-teste",
                     "fonte": "wokwi", "device_id": "esp32-teste"}
    assert verify == cfg.ca                                  # so a CA do projeto; nunca False
    assert (s.access, s.refresh_token, s.user_id) == ("A9", "R9", USER_ID)


def test_login_nao_guarda_o_segredo(servidor_falso, cfg):
    servidor_falso.respostas = [tokens("A9", "R9")]
    s = Sessao("esp32-teste", cfg)
    s.login("bridge-teste", "segredo-secreto-123")
    assert "segredo-secreto-123" not in repr(vars(s))


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_login_recusado_levanta_erro_com_o_codigo(servidor_falso, cfg, status):
    servidor_falso.respostas = [RespostaFalsa(status)]
    with pytest.raises(ErroServidor) as e:
        Sessao("esp32-teste", cfg).login("x", "y")
    assert e.value.status == status


def test_login_com_servidor_fora_levanta_indisponivel(servidor_falso, cfg):
    servidor_falso.respostas = [requests.ConnectionError("fora do ar")]
    with pytest.raises(ServidorIndisponivel):
        Sessao("esp32-teste", cfg).login("x", "y")


def test_renovar_com_refresh_200_aplica_os_tokens(sessao, servidor_falso):
    servidor_falso.respostas = [tokens("A2", "R2")]
    sessao.renovar()
    (rota, corpo, _), = servidor_falso.chamadas
    assert rota == "/auth/refresh" and corpo == {"schemaVersion": 3, "refresh_token": "R1"}
    assert (sessao.access, sessao.refresh_token) == ("A2", "R2")


@pytest.mark.parametrize("status", [400, 401, 403])
def test_refresh_recusado_pede_novo_login(sessao, servidor_falso, status):
    """Sem o segredo, o bridge nao refaz o login: o dispositivo precisa mandar o login de novo."""
    servidor_falso.respostas = [RespostaFalsa(status)]
    with pytest.raises(Relogin):
        sessao.renovar()
    assert sessao.precisa_login and sessao.access is None


@pytest.mark.parametrize("resposta", [RespostaFalsa(503), requests.ConnectionError("fora do ar")])
def test_servidor_ou_banco_fora_nao_derruba_a_sessao(sessao, servidor_falso, resposta):
    servidor_falso.respostas = [resposta]
    with pytest.raises((ErroServidor, ServidorIndisponivel)):
        sessao.renovar()
    assert not sessao.precisa_login and sessao.access == "A1"


def test_renovar_token_ja_trocado_por_outra_thread_nao_renova_de_novo(sessao, servidor_falso):
    sessao.access = "A2"                          # outra thread ja renovou; o refresh token R1 e de uso unico
    sessao.renovar(token_recusado="A1")
    assert servidor_falso.chamadas == []


def test_enviar_com_401_renova_e_repete_uma_vez(sessao, servidor_falso):
    servidor_falso.respostas = [RespostaFalsa(401), tokens("A2", "R2"), RespostaFalsa(200)]
    sessao.enviar("/dados", {"eventType": "imobilidade.leitura"})
    assert [r for r, _, _ in servidor_falso.chamadas] == ["/dados", "/auth/refresh", "/dados"]


def test_enviar_sem_login_pede_relogin(cfg):
    with pytest.raises(Relogin):
        Sessao("esp32-teste", cfg).enviar("/dados", {})


def test_headers_usam_o_access_atual(sessao):
    assert sessao.headers() == {"Authorization": "Bearer A1"}
