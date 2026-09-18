"""
Ponte entre a simulação Wokwi (rodando no VS Code) e o servidor unificado
(SensorServer/server.py).

Como funciona:
  1. O Wokwi for VS Code expõe a porta serial do ESP32 simulado via
     RFC2217 (configurado em Sketch/wokwi.toml: rfc2217ServerPort = 4000).
     Esse recurso é de depuração da extensão e funciona com a licença
     gratuita/Community (não depende do Private IoT Gateway pago).
  2. Este script conecta nessa porta serial local, lê linha por linha
     (o firmware já imprime um JSON por linha via Serial.println),
     anexa a identificação do usuário (user_id) e da fonte (source),
     CIFRA o evento (AES-256-GCM, mesmo mecanismo do app Android - ver
     crypto_utils.py) e envia (POST) o envelope cifrado para o servidor.

Pré-requisitos:
  - A simulação já estar rodando no VS Code (F1 -> Wokwi: Start Simulator),
    com a aba do simulador VISÍVEL (senão a simulação pausa).
  - O servidor (SensorServer/server.py) já estar rodando.

Uso:
  pip install -r requirements.txt
  python bridge.py
"""

import json
import sys
import time

import requests
import serial

from crypto_utils import encrypt_payload

# --- Configurações ---------------------------------------------------
RFC2217_URL = "rfc2217://localhost:4000"
BAUDRATE = 115200
SERVIDOR_URL = "http://localhost:5000/dados"
RECONECTAR_A_CADA_S = 3  # tempo de espera para tentar reconectar em caso de erro

# ---------------------------------------------------------------------
# ID do usuário (idoso) dono deste dispositivo wokwi.
#
# IMPORTANTE: precisa ser EXATAMENTE o mesmo número inteiro configurado
# no app Android (tela "Configurações", campo "ID do usuário"), para que
# o servidor entenda que os dados do celular e do wokwi pertencem à mesma
# pessoa e os salve juntos em dataUsers/usuario_<ID>.jsonl.
#
# Se este bridge estiver simulando o wokwi de outro idoso, basta trocar
# este valor (não precisa reiniciar a simulação no Wokwi, só o bridge).
# ---------------------------------------------------------------------
WOKWI_USER_ID = 1


def conectar_serial():
    """Tenta conectar na porta serial exposta pelo Wokwi via RFC2217.
    Fica tentando até a simulação estar de pé."""
    while True:
        try:
            ser = serial.serial_for_url(RFC2217_URL, baudrate=BAUDRATE, timeout=1)
            print(f"[bridge] Conectado à porta serial do Wokwi ({RFC2217_URL})")
            return ser
        except Exception as e:
            print(f"[bridge] Não foi possível conectar em {RFC2217_URL} ainda "
                  f"({e}). A simulação já está rodando no VS Code? "
                  f"Tentando novamente em {RECONECTAR_A_CADA_S}s...")
            time.sleep(RECONECTAR_A_CADA_S)


def enviar_para_servidor(envelope: dict):
    try:
        resp = requests.post(SERVIDOR_URL, json=envelope, timeout=5)
        if resp.status_code >= 300:
            print(f"[bridge] Servidor respondeu {resp.status_code}: {resp.text}")
    except requests.RequestException as e:
        print(f"[bridge] Falha ao enviar para o servidor ({SERVIDOR_URL}): {e}")


def main():
    ser = conectar_serial()

    print(f"[bridge] user_id configurado para este wokwi: {WOKWI_USER_ID}")
    print("[bridge] Lendo eventos da simulação, cifrando e enviando para "
          f"{SERVIDOR_URL} ... (Ctrl+C para sair)")

    while True:
        try:
            linha_bruta = ser.readline()
        except Exception as e:
            print(f"[bridge] Conexão serial perdida ({e}). Reconectando...")
            ser = conectar_serial()
            continue

        if not linha_bruta:
            continue  # timeout de leitura, tenta de novo

        linha = linha_bruta.decode(errors="ignore").strip()
        if not linha:
            continue

        # O sketch imprime uma linha de boas-vindas e mensagens de [DEBUG]
        # que não são JSON (ex.: toggles de botão) - ignoramos essas e
        # repassamos só os eventos estruturados.
        if not linha.startswith("{"):
            print(f"[bridge] (ignorado, não é JSON) {linha}")
            continue

        try:
            evento = json.loads(linha)
        except json.JSONDecodeError:
            print(f"[bridge] (ignorado, JSON inválido) {linha}")
            continue

        # Anexa identificação do usuário e da fonte ANTES de cifrar, para
        # que o servidor consiga rotear/agregar sem precisar decifrar
        # heurísticas de formato.
        evento["user_id"] = WOKWI_USER_ID
        evento["source"] = "wokwi"

        print(f"[bridge] Evento capturado (user_id={WOKWI_USER_ID}): {evento}")

        # Cifra o evento inteiro (mesmo mecanismo do app: AES-256-GCM,
        # chave compartilhada fixa - ver crypto_utils.py). O servidor
        # decifra o envelope da mesma forma para as duas fontes.
        nonce_b64, cipher_b64 = encrypt_payload(json.dumps(evento))
        envelope = {"nonce": nonce_b64, "ciphertext": cipher_b64}

        enviar_para_servidor(envelope)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[bridge] Encerrado pelo usuário.")
        sys.exit(0)
