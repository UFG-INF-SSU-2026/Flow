"""
Adaptador em software: um dispositivo simulado sem Wokwi nem placa, para demonstrar varios dispositivos de uma vez.

Faz o que o firmware faz: manda o login, emite leituras de acelerometro (normal, impacto e repouso), responde push.ack ao
pedido de confirmacao e, depois de 1,5 s, confirmacao.resposta ESTOU_BEM (o clique no botao).

  FLOW_SIM_SEGREDO=<senha da conta> python -m adaptadores.software --device-id sim-a --identificador bridge1 --queda
  (varios processos, cada um com a sua conta, device-id e porta: N dispositivos para 1 bridge)

O servidor tem cooldown de 60 s entre episodios: para repetir a queda, aguarde ou reinicie o servidor.
"""
import argparse
import os
import sys
import threading
import time

import requests

from adaptadores.cliente import ApiBridge, ErroApi, url_e_ca


class DispositivoSimulado:
    def __init__(self, api: ApiBridge, device_id: str, entity_id: str = "idoso-simulado", responde: bool = True):
        self.api, self.device_id, self.entity_id, self.responde = api, device_id, entity_id, responde
        self.seq = 0
        self.comandos_recebidos: list[str] = []
        self.parar = threading.Event()

    def evento(self, **kw) -> dict:
        self.seq += 1
        base = {"schemaVersion": 3, "deviceId": self.device_id, "entityId": self.entity_id, "eventTimeMs": self.seq * 1000,
                "sequence": self.seq + int(time.time()), "unit": "m/s2"}
        return {**base, **kw}

    def enviar(self, **kw) -> None:
        try:
            self.api.evento(self.evento(**kw))
        except (ErroApi, requests.RequestException) as e:
            print(f"[sim:{self.device_id}] evento nao enviado: {e if isinstance(e, ErroApi) else type(e).__name__}", flush=True)

    def tratar(self, cmd: str) -> None:
        self.comandos_recebidos.append(cmd)
        p = cmd.split(";")
        if len(p) >= 3 and p[1] == "BUZZER_ON":
            self.enviar(eventType="push.ack", state="ATIVADO", messageId=p[2])
            if self.responde:
                threading.Timer(1.5, lambda: self.enviar(eventType="confirmacao.resposta", state="ESTOU_BEM",
                                                         messageId=p[2])).start()

    def laco_comandos(self) -> None:
        while not self.parar.is_set():
            try:
                for cmd in self.api.comandos(espera=5):
                    self.tratar(cmd)
            except (ErroApi, requests.RequestException):
                time.sleep(1)

    def rodar(self, queda: bool) -> None:
        threading.Thread(target=self.laco_comandos, daemon=True).start()
        leituras = ([(1.0, 1.5)] * 2 + [(3.0, 25.0)] + [(0.3, 0.4)] * 10) if queda else [(1.0, 1.5)] * 1000
        for valor, pico in leituras:
            if self.parar.is_set():
                break
            self.enviar(eventType="imobilidade.leitura", state="NORMAL", value=valor, valueMax=pico)
            time.sleep(1)
        time.sleep(3)                                        # espera o pedido de confirmacao e a resposta


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Dispositivo simulado em software, para o bridge")
    ap.add_argument("--device-id", required=True, help="deviceId deste dispositivo (precisa estar na conta, se o servidor limitar)")
    ap.add_argument("--identificador", required=True, help="conta do tipo dispositivo (ex.: bridge1)")
    ap.add_argument("--queda", action="store_true", help="simula impacto seguido de repouso (dispara o pedido de confirmacao)")
    ap.add_argument("--sem-resposta", action="store_true", help="nao responde ao pedido de confirmacao")
    a = ap.parse_args(argv)
    segredo = os.environ.get("FLOW_SIM_SEGREDO", "")             # do ambiente, nunca de argumento de linha de comando
    if not segredo:
        sys.exit("Defina FLOW_SIM_SEGREDO (a senha da conta) no ambiente deste processo.")
    api = ApiBridge(*url_e_ca())
    api.login({"identificador": a.identificador, "segredo": segredo, "deviceId": a.device_id})
    print(f"[sim:{a.device_id}] login aceito", flush=True)
    DispositivoSimulado(api, a.device_id, responde=not a.sem_resposta).rodar(a.queda)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, ErroApi) as e:
        sys.exit(0 if isinstance(e, KeyboardInterrupt) else f"login nao aceito: {e}")
