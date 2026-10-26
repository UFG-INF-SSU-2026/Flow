"""
Teste do gateway SEM Wokwi: um 'firmware falso' no lugar da serial.

Ele emite leituras como o ESP32 faria (impacto + repouso), responde 'push.ack' ao receber CMD;BUZZER_ON e,
depois de 1,5 s, 'confirmacao.resposta ESTOU_BEM' (o clique no botao). Verifica o que o bridge escreveu na serial.

Pre-requisitos: servidor rodando, conta do bridge criada, variaveis FLOW_BRIDGE_ID / FLOW_BRIDGE_SECRET / FLOW_SERVIDOR.
ATENCAO: o servidor tem COOLDOWN de 60 s entre episodios; para repetir o teste, reinicie o servidor ou aguarde.

Uso (na pasta WokwiBridge):   python tests/fake_firmware.py
"""
import json
import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bridge  # noqa: E402


class FirmwareFalso:
    def __init__(self):
        self.rx = queue.Queue()          # o que o 'ESP32' imprime na serial (bridge le)
        self.escritas = []               # o que o bridge escreveu na serial (comandos)
        self.seq = 0
        self.msg_ativa = None
        self.pronto = threading.Event()  # o bridge so le a serial DEPOIS de autenticar

    def _linha(self, **kw):
        self.seq += 1
        base = {"schemaVersion": 3, "deviceId": "esp32-falso", "entityId": "idoso-simulado",
                "eventTimeMs": self.seq * 1000, "sequence": self.seq + int(time.time()), "unit": "m/s2"}
        return (json.dumps({**base, **kw}) + "\n").encode()

    # --- interface de serial usada pelo bridge ---
    def readline(self):
        self.pronto.set()
        try:
            return self.rx.get(timeout=0.3)
        except queue.Empty:
            return b""

    def write(self, dados):
        txt = dados.decode().strip()
        self.escritas.append(txt)
        p = txt.split(";")
        if len(p) >= 3 and p[1] == "BUZZER_ON":
            self.msg_ativa = p[2]
            self.rx.put(self._linha(eventType="push.ack", state="ATIVADO", messageId=p[2]))
            threading.Timer(1.5, lambda: self.rx.put(self._linha(
                eventType="confirmacao.resposta", state="ESTOU_BEM", messageId=p[2]))).start()


def ok(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


fw = FirmwareFalso()
threading.Thread(target=bridge.main, kwargs={"abrir_serial": lambda: fw}, daemon=True).start()
if not fw.pronto.wait(30):
    ok(False, "bridge nao ficou pronto em 30 s (login falhou? servidor no ar? contas criadas?)")
time.sleep(1)  # tempo para o canal de push abrir
print("Emitindo leituras: normal, IMPACTO, repouso...")
fw.rx.put(fw._linha(eventType="imobilidade.leitura", state="NORMAL", value=1.0, valueMax=1.5)); time.sleep(1)
fw.rx.put(fw._linha(eventType="imobilidade.leitura", state="NORMAL", value=3.0, valueMax=25.0)); time.sleep(1)
for _ in range(8):
    fw.rx.put(fw._linha(eventType="imobilidade.leitura", state="NORMAL", value=0.3, valueMax=0.4)); time.sleep(1)
    if any(e.startswith("CMD;BUZZER_OFF") for e in fw.escritas):
        break
time.sleep(1)
on = [e for e in fw.escritas if e.startswith("CMD;BUZZER_ON")]
off = [e for e in fw.escritas if e.startswith("CMD;BUZZER_OFF")]
ok(len(on) == 1, f"bridge escreveu CMD;BUZZER_ON na serial: {on}")
ok(len(on[0].split(";")) == 4 and 3000 < int(on[0].split(";")[3]) <= 5000, "comando com messageId e timeoutMs ~5000")
ok(len(off) == 1 and off[0].endswith(";CONFIRMADO"), f"apos o 'clique', bridge escreveu CMD;BUZZER_OFF;...;CONFIRMADO: {off}")
print("\nGATEWAY OK")
