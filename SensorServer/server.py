"""
Servidor centralizado de dados de sensores (Marco 3 - integracao ubiqua).

Agora o mesmo servidor recebe dados de DUAS fontes diferentes, que podem
pertencer ao MESMO usuario (o mesmo idoso pode estar com o celular e com um
dispositivo wokwi ao mesmo tempo):

    App Android (SensorApp)
        -> cifra o JSON (AES-256-GCM, ver crypto_utils.py)
        -> POST /dados  {"nonce": ..., "ciphertext": ...}
        -> payload decifrado inclui "user_id" (int) e "source": "app"

    Wokwi (via WokwiBridge/bridge.py)
        -> bridge le a serial do ESP32 simulado, anexa "user_id" e
           "source": "wokwi" ao evento, cifra (mesma chave/algoritmo) e
        -> POST /dados  {"nonce": ..., "ciphertext": ...}

Como as duas fontes agora enviam a cada 1 segundo (mas de forma
assincrona, sem sincronizacao entre si), o servidor NAO grava um arquivo
por requisicao. Em vez disso:

    1. Cada payload decifrado que chega e guardado em um buffer em memoria,
       na chave do "user_id", sobrescrevendo a leitura mais recente daquela
       fonte ("app" ou "wokwi") dentro da janela atual.
    2. Uma thread de fundo "dispara" a cada 1 segundo (janela fixa) e, para
       cada usuario que recebeu QUALQUER dado desde o ultimo disparo, grava
       UMA linha no arquivo dataUsers/usuario_<user_id>.jsonl, no formato:

           {
             "timestamp": "<hora do flush, ISO 8601>",
             "user_id": <int>,
             "app": {...} | null,
             "wokwi": {...} | null
           }

       Se uma das fontes nao mandou nada naquela janela de 1s, a chave dela
       fica como null (nao repete o ultimo valor conhecido).

Alem disso, o fluxo priorizado do Marco 2 (LeituraAmbiente -> regra de luz
inadequada -> alerta simulado para o cuidador) continua funcionando
normalmente para dados vindos do app, de forma independente do buffer acima
(processado assim que o payload chega, sem esperar o flush de 1s).

Como rodar:
    pip install -r requirements.txt
    python server.py
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from flask import Flask, request, jsonify

from crypto_utils import decrypt_payload, DecryptionError
from regra_luz import validar_evento, eh_duplicado, avaliar_regra, EventoInvalido
import regra_imobilidade

# ---------------------------------------------------------------------------
# CONFIGURACOES (edite aqui se precisar)
# ---------------------------------------------------------------------------
PORT = 5000                 # Porta em que o servidor vai escutar
DATA_DIR = "dataUsers"      # Pasta onde os .jsonl por usuario (e os alertas) sao salvos
JANELA_AGREGACAO_S = 1.0    # Tamanho da janela de agregacao app+wokwi (segundos)
FONTES_VALIDAS = {"app", "wokwi"}
# ---------------------------------------------------------------------------

app = Flask(__name__)

os.makedirs(DATA_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Buffer de agregacao por usuario (protegido por lock, pois a thread de
# flush roda em paralelo com as requisicoes HTTP recebidas pelo Flask).
#
#   _buffer[user_id] = {"app": <payload ou None>, "wokwi": <payload ou None>}
# ---------------------------------------------------------------------------
_buffer_lock = threading.Lock()
_buffer: dict[int, dict[str, dict | None]] = {}

# Guarda os ultimos registros gravados, so para o GET /dados de debug.
MAX_EM_MEMORIA = 200
_registros_recentes: list[dict] = []


def _agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _arquivo_usuario(user_id: int) -> str:
    return os.path.join(DATA_DIR, f"usuario_{user_id}.jsonl")


def _registrar_no_buffer(user_id: int, source: str, dados: dict) -> None:
    """Guarda a leitura mais recente de 'source' para 'user_id' na janela atual."""
    with _buffer_lock:
        slot = _buffer.setdefault(user_id, {"app": None, "wokwi": None})
        slot[source] = dados


def _flush_loop() -> None:
    """Roda em thread separada: a cada JANELA_AGREGACAO_S segundos, grava uma
    linha por usuario que recebeu dado (de app e/ou wokwi) na janela."""
    while True:
        time.sleep(JANELA_AGREGACAO_S)

        with _buffer_lock:
            _buffer_local_copy = {uid: dict(v) for uid, v in _buffer.items()}
            _buffer.clear()

        for user_id, fontes in _buffer_local_copy.items():
            dado_app = fontes.get("app")
            dado_wokwi = fontes.get("wokwi")

            if dado_app is None and dado_wokwi is None:
                continue  # nada chegou dessa vez para esse usuario

            registro = {
                "timestamp": _agora_iso(),
                "user_id": user_id,
                "app": dado_app,
                "wokwi": dado_wokwi,
            }

            filepath = _arquivo_usuario(user_id)
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(registro, ensure_ascii=False) + "\n")

            _registros_recentes.append(registro)
            if len(_registros_recentes) > MAX_EM_MEMORIA:
                _registros_recentes.pop(0)

            print(
                f"[{registro['timestamp']}] usuario={user_id} "
                f"app={'ok' if dado_app else 'null'} "
                f"wokwi={'ok' if dado_wokwi else 'null'} -> {filepath}"
            )


def _processar_leitura_ambiente(payload: dict, device_id: str) -> None:
    """Fluxo priorizado (Marco 2): LeituraAmbiente -> regra de luz -> alerta.
    So se aplica a payloads vindos do app (unico que emite esse evento)."""
    evento_ambiente = payload.get("leitura_ambiente")
    if evento_ambiente is None:
        return

    try:
        validar_evento(evento_ambiente)
    except EventoInvalido as e:
        print(f"  [leitura_ambiente] descartada (invalida): {e}")
        return

    if eh_duplicado(evento_ambiente):
        print(f"  [leitura_ambiente] descartada (duplicada, seq_num={evento_ambiente['seq_num']})")
        return

    resultado_regra = avaliar_regra(evento_ambiente)
    if resultado_regra is not None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"alerta_{device_id}_{timestamp}.json"
        filepath = os.path.join(DATA_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(resultado_regra, f, ensure_ascii=False, indent=2)
        print(f"  >>> ALERTA gerado: {filename} -> {resultado_regra['mensagem']}")


def _processar_evento_wokwi(payload: dict, device_id: str) -> None:
    """Fecha a pendencia do relatorio (secao 'Consumidor'): cascata de
    validacao por eventType + tabela de estado persistente + atuacao para
    imobilidade.decisao com state=ALERTA_IMOBILIDADE. Espelha o fluxo
    priorizado de _processar_leitura_ambiente, mas para eventos do wokwi."""
    try:
        regra_imobilidade.validar_evento(payload)
    except regra_imobilidade.EventoInvalido as e:
        print(f"  [wokwi] evento descartado (invalido): {e}")
        return

    if regra_imobilidade.eh_duplicado(payload):
        print(f"  [wokwi] evento descartado (duplicado/atrasado, sequence={payload.get('sequence')})")
        return

    alerta = regra_imobilidade.processar_evento(payload)
    if alerta is not None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"alerta_imobilidade_{device_id}_{timestamp}.json"
        filepath = os.path.join(DATA_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(alerta, f, ensure_ascii=False, indent=2)
        print(f"  >>> ALERTA gerado: {filename} -> {alerta['mensagem']}")


@app.route("/dados", methods=["POST"])
def receber_dados():
    envelope = request.get_json(silent=True)

    if envelope is None or "nonce" not in envelope or "ciphertext" not in envelope:
        return jsonify({
            "status": "erro",
            "mensagem": "Corpo invalido: esperado {'nonce': ..., 'ciphertext': ...}"
        }), 400

    # 1. Decifrar o envelope (transporte) - mesmo mecanismo para app e wokwi
    try:
        payload_texto = decrypt_payload(envelope["nonce"], envelope["ciphertext"])
        payload = json.loads(payload_texto)
    except (DecryptionError, json.JSONDecodeError) as e:
        return jsonify({"status": "erro", "mensagem": f"Falha ao decifrar/parsear: {e}"}), 400

    # 2. Identificar usuario (dono do dado) e fonte (app ou wokwi)
    source = payload.get("source")
    user_id_raw = payload.get("user_id")

    if source not in FONTES_VALIDAS:
        return jsonify({
            "status": "erro",
            "mensagem": f"Campo 'source' ausente ou invalido (esperado 'app' ou 'wokwi'): {source}"
        }), 400

    try:
        user_id = int(user_id_raw)
    except (TypeError, ValueError):
        return jsonify({
            "status": "erro",
            "mensagem": f"Campo 'user_id' ausente ou nao-inteiro: {user_id_raw}"
        }), 400

    # payload especifico da fonte, sem os campos de roteamento (ja estao no
    # nivel superior do registro gravado: user_id/timestamp)
    dados_fonte = {k: v for k, v in payload.items() if k not in ("source", "user_id")}

    device_id = str(payload.get("device_id") or payload.get("deviceId") or f"user{user_id}")

    print(f"[{_agora_iso()}] Dados decifrados: user_id={user_id} source={source} device_id={device_id}")

    # 3. Guarda no buffer de agregacao (vira 1 linha no jsonl do usuario no
    #    proximo flush de 1s, junto com a outra fonte, se houver)
    _registrar_no_buffer(user_id, source, dados_fonte)

    # 4. Fluxo priorizado (independente do buffer): regra de luz inadequada
    #    (app) ou validacao/estado/atuacao de imobilidade (wokwi)
    if source == "app":
        _processar_leitura_ambiente(payload, device_id)
    elif source == "wokwi":
        _processar_evento_wokwi(payload, device_id)

    return jsonify({
        "status": "ok",
        "user_id": user_id,
        "source": source,
    }), 200


@app.route("/dados", methods=["GET"])
def listar_dados():
    """Debug: lista os ultimos registros ja gravados (pos-flush)."""
    return jsonify(_registros_recentes)


@app.route("/estado/<device_id>", methods=["GET"])
def obter_estado_imobilidade(device_id):
    """Debug/demo: expoe a tabela de estado persistente (nao o buffer de
    1s) de um dispositivo wokwi - evidencia observavel de 'mudanca de
    estado', separada do log em dataUsers/usuario_<id>.jsonl."""
    estado = regra_imobilidade.obter_estado(device_id)
    if estado is None:
        return jsonify({"status": "erro", "mensagem": f"device_id desconhecido: {device_id}"}), 404
    return jsonify(estado)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/", methods=["GET"])
def status():
    # Endpoint simples para o app/bridge checarem se o servidor esta de pe
    return jsonify({"status": "online"}), 200


if __name__ == "__main__":
    print(f"Servidor iniciando na porta {PORT}...")
    print(f"Salvando dados recebidos em: {os.path.abspath(DATA_DIR)} (1 arquivo .jsonl por usuario)")
    print(f"Janela de agregacao app+wokwi: {JANELA_AGREGACAO_S}s")
    print("Transporte: AES-256-GCM (chave compartilhada fixa, ver crypto_utils.py)")

    flush_thread = threading.Thread(target=_flush_loop, daemon=True)
    flush_thread.start()

    # host="0.0.0.0" para aceitar conexoes de outros dispositivos na mesma rede
    app.run(host="0.0.0.0", port=PORT)
