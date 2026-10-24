"""
Simula um 'wokwi' que sofre um impacto e fica imovel, para testar o push de confirmacao SEM Wokwi nem celular.

Faz login com a conta do bridge, envia telemetria 1x/s (como o ESP32 faria) e, apos o impacto, 5 s de repouso.
A analise (python -m analise) deve concluir 'queda provavel' e gravar o pedido; a borda (python -m borda) o empurra para os
canais conectados (app e/ou bridge). Os DOIS processos precisam estar no ar.

Uso (na pasta SensorServer, com a borda e a analise rodando):
    pip install requests
    FLOW_BRIDGE_ID=bridge1 FLOW_BRIDGE_SECRET=<senha> python simular_queda.py [URL_BASE]
"""
import json
import os
import sys
import time

import requests

from comum import const as const_comum
from borda.crypto_utils import decrypt_payload, encrypt_payload

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5000").rstrip("/")
CONTA = os.environ.get("FLOW_BRIDGE_ID", "bridge1")
SENHA = os.environ["FLOW_BRIDGE_SECRET"]


def env(obj):
    n, c = encrypt_payload(json.dumps(obj))
    return {"nonce": n, "ciphertext": c}


r = requests.post(f"{BASE}/auth/login", json=env({"schemaVersion": const_comum.SCHEMA_VERSION_AUTH, "identificador": CONTA, "segredo": SENHA,
                                                  "fonte": "wokwi", "device_id": "simulador-queda"}))
r.raise_for_status()
j = r.json()
tok = json.loads(decrypt_payload(j["nonce"], j["ciphertext"]))
H = {"Authorization": f"Bearer {tok['access_token']}"}
user_id = tok["user_ids"][0]
seq = int(time.time())


def enviar(value, value_max):
    global seq
    seq += 1
    ev = {"schemaVersion": const_comum.SCHEMA_VERSION_WOKWI, "eventType": "imobilidade.leitura", "deviceId": "simulador-queda", "entityId": "idoso-simulado",
          "eventTimeMs": seq * 1000, "sequence": seq, "value": value, "valueMax": value_max, "unit": "m/s2",
          "state": "NORMAL", "user_id": user_id, "source": "wokwi"}
    rr = requests.post(f"{BASE}/dados", json=env(ev), headers=H)
    print(f"  value={value:5.1f} valueMax={value_max:5.1f} -> HTTP {rr.status_code}")


print(f"Simulando queda para user_id={user_id}...")
for _ in range(2):
    enviar(1.0, 1.5); time.sleep(1)
print("IMPACTO"); enviar(3.0, 25.0); time.sleep(1)
print("Imovel por 5 s (o pedido deve chegar aos canais apos ~3 s)")
for _ in range(5):
    enviar(0.3, 0.4); time.sleep(1)
print("Fim. Confira o app/bridge e as tabelas comandos_push, episodios e confirmacoes (user_id=%d)." % user_id)
