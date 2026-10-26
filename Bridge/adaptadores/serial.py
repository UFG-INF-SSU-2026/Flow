"""
Adaptador serial: liga um dispositivo na serial (Wokwi, Arduino, ESP32 na USB) a API do bridge. Um processo por dispositivo.

  python -m adaptadores.serial --nome sim1 --porta rfc2217://localhost:4000      (Wokwi, rfc2217ServerPort em wokwi.toml)
  python -m adaptadores.serial --nome placa --porta COM3                         (placa real; /dev/ttyUSB0 no Linux)

Protocolo de linhas (o mesmo do sketch/):
  dispositivo -> adaptador   {"eventType":"login","identificador":...,"segredo":...,"deviceId":...}   entrega o login ao bridge
                             {"eventType":..., ...}                                                   evento (um JSON por linha)
                             qualquer outra linha (boas-vindas, [DEBUG]) e ignorada
  adaptador -> dispositivo   CMD;BUZZER_ON;<messageId>;<timeoutMs> | CMD;BUZZER_OFF;<messageId>;<resultado>
                             CMD;LOGIN    pede que o dispositivo mande o login (ao conectar, ou quando o bridge pediu relogin)

O adaptador nao tem credencial: o segredo mora no firmware e so passa por aqui, no trecho local (serial ou USB), a caminho do
bridge. Nunca e impresso nem guardado. Ambiente: FLOW_BRIDGE_URL (padrao https://127.0.0.1:5443) e FLOW_TLS_CA.
"""
import argparse
import json
import sys
import threading
import time

import requests
import serial

from adaptadores.cliente import ApiBridge, ErroApi, url_e_ca

BAUDRATE = 115200
RECONECTAR_A_CADA_S = 3
ESPERA_COMANDO_S = 20
PEDIR_LOGIN_A_CADA_S = 5


def log(nome: str, msg: str) -> None:
    print(f"[serial:{nome}] {msg}", flush=True)


class Porta:
    """A conexao serial pode ser recriada pela reconexao; as duas threads usam sempre a ATUAL."""

    def __init__(self):
        self.ser = None
        self._wlock = threading.Lock()

    def escrever(self, linha: str) -> bool:
        with self._wlock:
            if self.ser is None:
                return False
            try:
                self.ser.write((linha + "\n").encode())
                return True
            except Exception:
                return False


def processar_linha(nome: str, api: ApiBridge, porta: Porta, linha: str) -> None:
    """Uma linha vinda do dispositivo: login, evento ou ruido."""
    if "segredo" in linha and linha.startswith("{"):          # linha de login: nunca vai para o log
        try:
            api.login(json.loads(linha))
            log(nome, f"login aceito (deviceId={api.device_id})")
        except (ErroApi, requests.RequestException, ValueError, KeyError) as e:
            log(nome, f"login nao aceito: {e if isinstance(e, ErroApi) else type(e).__name__}")
        return
    if not linha.startswith("{"):
        log(nome, f"(ignorado, nao e JSON) {linha}")
        return
    try:
        evento = json.loads(linha)
    except json.JSONDecodeError:
        log(nome, f"(ignorado, JSON invalido) {linha}")
        return
    if not isinstance(evento, dict):
        log(nome, f"(ignorado, JSON nao e objeto) {linha}")
        return
    if api.token is None:
        porta.escrever("CMD;LOGIN")                          # ainda sem login: pede ao dispositivo
        return
    try:
        api.evento(evento)
    except ErroApi as e:
        if e.status == 401:                                  # relogin: o bridge nao guarda o segredo
            api.esquecer()
            porta.escrever("CMD;LOGIN")
        log(nome, f"evento recusado: {e}")
    except requests.RequestException as e:
        log(nome, f"bridge indisponivel: {type(e).__name__}")  # sem fila de retransmissao


def laco_comandos(nome: str, api: ApiBridge, porta: Porta) -> None:
    while True:
        if api.token is None:
            time.sleep(0.5)
            continue
        try:
            for cmd in api.comandos(ESPERA_COMANDO_S):
                ok = porta.escrever(cmd)
                log(nome, f"comando -> serial {'OK' if ok else 'FALHOU'}: {cmd}")
        except ErroApi as e:
            if e.status == 401:
                api.esquecer()
                porta.escrever("CMD;LOGIN")
            time.sleep(1)
        except requests.RequestException:
            time.sleep(RECONECTAR_A_CADA_S)


def conectar(nome: str, url_porta: str):
    while True:
        try:
            ser = serial.serial_for_url(url_porta, baudrate=BAUDRATE, timeout=1)
            log(nome, f"conectado a porta {url_porta}")
            return ser
        except Exception as e:
            log(nome, f"nao foi possivel abrir {url_porta} ({type(e).__name__}); tentando de novo em {RECONECTAR_A_CADA_S}s")
            time.sleep(RECONECTAR_A_CADA_S)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Adaptador serial do bridge")
    ap.add_argument("--nome", required=True, help="rotulo deste dispositivo nos logs (a identidade vem do login do firmware)")
    ap.add_argument("--porta", default="rfc2217://localhost:4000", help="porta serial ou URL rfc2217 do Wokwi")
    a = ap.parse_args(argv)
    url, ca = url_e_ca()
    api, porta = ApiBridge(url, ca), Porta()
    threading.Thread(target=laco_comandos, args=(a.nome, api, porta), daemon=True).start()
    porta.ser = conectar(a.nome, a.porta)
    ultimo_pedido = 0.0
    while True:
        if api.token is None and time.monotonic() - ultimo_pedido > PEDIR_LOGIN_A_CADA_S:
            porta.escrever("CMD;LOGIN")                      # o firmware pode ter ligado antes do adaptador
            ultimo_pedido = time.monotonic()
        try:
            bruta = porta.ser.readline()
        except Exception as e:
            log(a.nome, f"conexao serial perdida ({type(e).__name__}); reconectando")
            porta.ser = conectar(a.nome, a.porta)
            continue
        linha = bruta.decode(errors="ignore").strip() if bruta else ""
        if linha:
            processar_linha(a.nome, api, porta, linha)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
