"""Adaptador serial: o que faz com cada linha do dispositivo. API e serial sao dublês."""
import json

import pytest

from adaptadores import serial as adaptador
from adaptadores.cliente import ApiBridge, ErroApi


class PortaFalsa:
    def __init__(self):
        self.linhas = []

    def escrever(self, linha):
        self.linhas.append(linha)
        return True


class ApiFalsa:
    def __init__(self, token="T"):
        self.token, self.device_id, self.enviados, self.logins = token, "dev-a", [], []
        self.falha = None

    def login(self, linha):
        self.logins.append(linha)
        self.token = "T"

    def evento(self, evento):
        if self.falha:
            raise self.falha
        self.enviados.append(evento)

    def esquecer(self):
        self.token = None


def processar(api, porta, linha):
    adaptador.processar_linha("t", api, porta, linha)


@pytest.mark.parametrize("linha", ["MPU6050 encontrado e iniciado!", "[DEBUG] algo", "[1, 2, 3]",
                                   '{"eventType": "imobilidade.leitura"', '"texto"'])
def test_linha_que_nao_e_evento_json_e_ignorada(linha):
    api, porta = ApiFalsa(), PortaFalsa()
    processar(api, porta, linha)
    assert api.enviados == [] and porta.linhas == []


def test_evento_vai_para_a_api():
    api, porta = ApiFalsa(), PortaFalsa()
    processar(api, porta, json.dumps({"eventType": "push.ack", "state": "ATIVADO"}))
    assert api.enviados == [{"eventType": "push.ack", "state": "ATIVADO"}]


def test_evento_sem_login_pede_o_login_ao_dispositivo():
    api, porta = ApiFalsa(token=None), PortaFalsa()
    processar(api, porta, json.dumps({"eventType": "imobilidade.leitura"}))
    assert api.enviados == [] and porta.linhas == ["CMD;LOGIN"]


def test_relogin_da_api_pede_o_login_ao_dispositivo_de_novo():
    api, porta = ApiFalsa(), PortaFalsa()
    api.falha = ErroApi(401, "relogin")
    processar(api, porta, json.dumps({"eventType": "imobilidade.leitura"}))
    assert api.token is None and porta.linhas == ["CMD;LOGIN"]


def test_linha_de_login_vai_ao_bridge_e_nunca_para_o_log(capsys):
    api, porta = ApiFalsa(token=None), PortaFalsa()
    processar(api, porta, json.dumps({"eventType": "login", "identificador": "c", "segredo": "SEGREDO-X", "deviceId": "dev-a"}))
    assert api.logins[0]["segredo"] == "SEGREDO-X"
    assert "SEGREDO-X" not in capsys.readouterr().out


def test_login_com_linha_invalida_nao_vaza_o_segredo(capsys):
    api, porta = ApiFalsa(token=None), PortaFalsa()
    processar(api, porta, '{"segredo": "SEGREDO-Y", truncado')
    assert "SEGREDO-Y" not in capsys.readouterr().out


def test_porta_sem_conexao_devolve_false_e_escreve_com_quebra_de_linha():
    assert adaptador.Porta().escrever("CMD;BUZZER_OFF;x;CONFIRMADO") is False

    class SerialFalsa:
        gravado = b""

        def write(self, dados):
            self.gravado += dados

    p = adaptador.Porta()
    p.ser = SerialFalsa()
    assert p.escrever("CMD;BUZZER_OFF;x;CONFIRMADO") is True
    assert p.ser.gravado == b"CMD;BUZZER_OFF;x;CONFIRMADO\n"


def test_cliente_da_api_recusa_http():
    with pytest.raises(ValueError):
        ApiBridge("http://127.0.0.1:5443", "ca.crt")
