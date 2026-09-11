"""
Servidor centralizado de dados de sensores (Marco 2).

Fluxo implementado (fluxo priorizado: LeituraAmbiente -> regra CCT -> alerta):

    App Android
        -> cifra o JSON (AES-256-GCM, ver crypto_utils.py)
        -> POST /dados  {"nonce": ..., "ciphertext": ...}
    Servidor
        -> decifra o envelope
        -> salva o payload bruto decifrado em dataUsers/ (auditoria, como antes)
        -> se o payload tiver "leitura_ambiente":
            -> valida (schema + faixa fisica)
            -> descarta duplicado (device_id + seq_num)
            -> avalia a regra de luz inadequada (regra_luz.py)
            -> se confirmada: grava um arquivo de ALERTA em dataUsers/
               (simulação da notificação ao cuidador - nao ha app do
               cuidador implementado ainda, entao o "aviso" e este arquivo)

Como rodar:
    pip install -r requirements.txt
    python server.py
"""

import os
import json
from datetime import datetime
from flask import Flask, request, jsonify

from crypto_utils import decrypt_payload, DecryptionError
from regra_luz import validar_evento, eh_duplicado, avaliar_regra, EventoInvalido

# ---------------------------------------------------------------------------
# CONFIGURACOES (edite aqui se precisar)
# ---------------------------------------------------------------------------
PORT = 5000                 # Porta em que o servidor vai escutar
DATA_DIR = "dataUsers"      # Pasta onde os JSONs recebidos (e os alertas) sao salvos
# ---------------------------------------------------------------------------

app = Flask(__name__)

# Garante que a pasta de dados exista
os.makedirs(DATA_DIR, exist_ok=True)


def _timestamp_arquivo() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _salvar_json(prefixo: str, device_id: str, conteudo: dict) -> str:
    """Salva um dicionario como arquivo JSON em DATA_DIR e retorna o nome do arquivo."""
    timestamp = _timestamp_arquivo()
    filename = f"{prefixo}{device_id}_{timestamp}.json"
    filepath = os.path.join(DATA_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(conteudo, f, ensure_ascii=False, indent=2)
    return filename


@app.route("/dados", methods=["POST"])
def receber_dados():
    envelope = request.get_json(silent=True)

    if envelope is None or "nonce" not in envelope or "ciphertext" not in envelope:
        return jsonify({
            "status": "erro",
            "mensagem": "Corpo invalido: esperado {'nonce': ..., 'ciphertext': ...}"
        }), 400

    # 1. Decifrar o envelope (transporte)
    try:
        payload_texto = decrypt_payload(envelope["nonce"], envelope["ciphertext"])
        payload = json.loads(payload_texto)
    except (DecryptionError, json.JSONDecodeError) as e:
        return jsonify({"status": "erro", "mensagem": f"Falha ao decifrar/parsear: {e}"}), 400

    device_id = str(payload.get("device_id", "desconhecido"))

    # 2. Salva o payload bruto decifrado (auditoria/depuracao, como antes do Marco 2)
    arquivo_dados = _salvar_json("", device_id, payload)
    print(f"[{_timestamp_arquivo()}] Dados decifrados de '{device_id}' -> {arquivo_dados}")

    # 3. Processa o fluxo priorizado: LeituraAmbiente -> regra de luz inadequada
    evento_ambiente = payload.get("leitura_ambiente")
    resultado_regra = None

    if evento_ambiente is not None:
        try:
            validar_evento(evento_ambiente)
        except EventoInvalido as e:
            print(f"  [leitura_ambiente] descartada (invalida): {e}")
            evento_ambiente = None

    if evento_ambiente is not None:
        if eh_duplicado(evento_ambiente):
            print(f"  [leitura_ambiente] descartada (duplicada, seq_num={evento_ambiente['seq_num']})")
        else:
            resultado_regra = avaliar_regra(evento_ambiente)

    if resultado_regra is not None:
        arquivo_alerta = _salvar_json("alerta_", device_id, resultado_regra)
        print(f"  >>> ALERTA gerado: {arquivo_alerta} -> {resultado_regra['mensagem']}")

    return jsonify({
        "status": "ok",
        "arquivo": arquivo_dados,
        "alerta_gerado": resultado_regra is not None,
    }), 200


@app.route("/", methods=["GET"])
def status():
    # Endpoint simples para o app checar se o servidor esta de pe
    return jsonify({"status": "online"}), 200


if __name__ == "__main__":
    print(f"Servidor iniciando na porta {PORT}...")
    print(f"Salvando dados recebidos em: {os.path.abspath(DATA_DIR)}")
    print("Transporte: AES-256-GCM (chave compartilhada fixa, ver crypto_utils.py)")
    # host="0.0.0.0" para aceitar conexoes de outros dispositivos na mesma rede
    app.run(host="0.0.0.0", port=PORT)
