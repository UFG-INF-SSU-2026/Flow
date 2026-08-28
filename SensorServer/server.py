"""
Servidor centralizado de dados de sensores.

Recebe requisicoes POST em /dados contendo um JSON com os dados
dos sensores enviados pelo aplicativo Android, e salva cada
requisicao como um arquivo JSON individual dentro da pasta
'dataUsers'.

Como rodar:
    pip install flask
    python server.py
"""

import os
import json
from datetime import datetime
from flask import Flask, request, jsonify

# ---------------------------------------------------------------------------
# CONFIGURACOES (edite aqui se precisar)
# ---------------------------------------------------------------------------
PORT = 5000                 # Porta em que o servidor vai escutar
DATA_DIR = "dataUsers"      # Pasta onde os JSONs recebidos serao salvos
# ---------------------------------------------------------------------------

app = Flask(__name__)

# Garante que a pasta de dados exista
os.makedirs(DATA_DIR, exist_ok=True)


@app.route("/dados", methods=["POST"])
def receber_dados():
    payload = request.get_json(silent=True)

    if payload is None:
        return jsonify({"status": "erro", "mensagem": "JSON invalido ou ausente"}), 400

    # Identificador do dispositivo. Se o app nao mandar, usa "desconhecido".
    device_id = str(payload.get("device_id", "desconhecido"))

    # Timestamp de quando o SERVIDOR recebeu a mensagem (conforme especificado)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    filename = f"{device_id}_{timestamp}.json"
    filepath = os.path.join(DATA_DIR, filename)

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except OSError as e:
        return jsonify({"status": "erro", "mensagem": f"Falha ao salvar arquivo: {e}"}), 500

    print(f"[{timestamp}] Dados recebidos de '{device_id}' -> {filename}")

    return jsonify({"status": "ok", "arquivo": filename}), 200


@app.route("/", methods=["GET"])
def status():
    # Endpoint simples para o app checar se o servidor esta de pe
    return jsonify({"status": "online"}), 200


if __name__ == "__main__":
    print(f"Servidor iniciando na porta {PORT}...")
    print(f"Salvando dados recebidos em: {os.path.abspath(DATA_DIR)}")
    # host="0.0.0.0" para aceitar conexoes de outros dispositivos na mesma rede
    app.run(host="0.0.0.0", port=PORT)
