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
import re
import threading
import time
from datetime import datetime, timezone
from typing import NamedTuple
from flask import Flask, request, jsonify, g

import auth
import const   # PORT, DATA_DIR, JANELA_AGREGACAO_S, FONTES_VALIDAS... (ver const.py)
import mensagens
import regra_queda
import canal_app
import canal_wokwi
from authz import exige, exigir_escopo, ForaDeEscopo, negar
from crypto_utils import decrypt_payload, encrypt_payload, DecryptionError
from regra_luz import validar_evento, eh_duplicado, avaliar_regra, EventoInvalido
import regra_imobilidade

# Canais de push (servidor -> dispositivo). Todos expoem CANAL e enviar(user_id, mensagem).
CANAIS = [canal_app, canal_wokwi]
_SCHEMA_VERSION_AUSENTE_DADOS=1

app = Flask(__name__)
app.register_blueprint(canal_app.bp)
app.register_blueprint(canal_wokwi.bp)

os.makedirs(const.DATA_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Buffer de agregacao por usuario (protegido por lock, pois a thread de
# flush roda em paralelo com as requisicoes HTTP recebidas pelo Flask).
#
#   _buffer[user_id] = {"app": <payload ou None>, "wokwi": <payload ou None>}
# ---------------------------------------------------------------------------
_buffer_lock = threading.Lock()
_buffer: dict[int, dict[str, dict | None]] = {}

# Guarda os ultimos registros gravados (limite: const.MAX_EM_MEMORIA), so para o GET /dados de debug.
_registros_recentes: list[dict] = []


def _agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _arquivo_usuario(user_id: int) -> str:
    return os.path.join(const.DATA_DIR, f"usuario_{user_id}.jsonl")


def _registrar_no_buffer(user_id: int, source: str, dados: dict) -> None:
    """Guarda a leitura mais recente de 'source' para 'user_id' na janela atual."""
    with _buffer_lock:
        slot = _buffer.setdefault(user_id, {"app": None, "wokwi": None})
        slot[source] = dados


def _flush_loop() -> None:
    """Roda em thread separada: a cada const.JANELA_AGREGACAO_S segundos, grava uma
    linha por usuario que recebeu dado (de app e/ou wokwi) na janela."""
    while True:
        time.sleep(const.JANELA_AGREGACAO_S)

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
            if len(_registros_recentes) > const.MAX_EM_MEMORIA:
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
        filepath = os.path.join(const.DATA_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(resultado_regra, f, ensure_ascii=False, indent=2)
        print(f"  >>> ALERTA gerado: {filename} -> {resultado_regra['mensagem']}")


def _falha_fluxo_legado(nome: str, e: Exception) -> None:
    """Falha inesperada num fluxo legado (payload malformado ou bug): so loga. A resposta segue 200 (R-06)."""
    print(f"  [{nome}] falha no fluxo legado (ignorada): {type(e).__name__}: {str(e)[:200]}")


WOKWI_INVALIDO = "INVALIDO"
WOKWI_DUPLICADO = "DUPLICADO"
WOKWI_ACEITO = "ACEITO"


class ResultadoWokwi(NamedTuple):
    """Resultado de _processar_evento_wokwi. So ACEITO pode alimentar regra_queda (Marco 3, 9.1)."""
    estado: str                    # WOKWI_INVALIDO | WOKWI_DUPLICADO | WOKWI_ACEITO
    alerta: dict | None = None     # alerta de imobilidade (so quando ACEITO)


def _processar_evento_wokwi(payload: dict, device_id: str) -> ResultadoWokwi:
    """Fecha a pendencia do relatorio (secao 'Consumidor'): cascata de
    validacao por eventType + tabela de estado persistente + atuacao para
    imobilidade.decisao com state=ALERTA_IMOBILIDADE. Espelha o fluxo
    priorizado de _processar_leitura_ambiente, mas para eventos do wokwi.

    Devolve o estado explicito (INVALIDO | DUPLICADO | ACEITO) para que o chamador nao confunda
    'descartado' com 'aceito, sem alerta'."""
    try:
        regra_imobilidade.validar_evento(payload)
    except regra_imobilidade.EventoInvalido as e:
        print(f"  [wokwi] evento descartado (invalido): {e}")
        return ResultadoWokwi(WOKWI_INVALIDO)

    if regra_imobilidade.eh_duplicado(payload):
        print(f"  [wokwi] evento descartado (duplicado/atrasado, sequence={payload.get('sequence')})")
        return ResultadoWokwi(WOKWI_DUPLICADO)

    alerta = regra_imobilidade.processar_evento(payload)
    if alerta is not None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"alerta_imobilidade_{device_id}_{timestamp}.json"
        filepath = os.path.join(const.DATA_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(alerta, f, ensure_ascii=False, indent=2)
        print(f"  >>> ALERTA gerado: {filename} -> {alerta['mensagem']}")

    return ResultadoWokwi(WOKWI_ACEITO, alerta)


# ---------------------------------------------------------------------------
# Marco 3: decisao de queda e push
# ---------------------------------------------------------------------------
def _despachar(user_id: int, msg: dict) -> None:
    """Entrega a mesma mensagem a TODOS os canais; registra o resultado por canal."""
    for canal in CANAIS:
        resultado = canal.enviar(user_id, msg)
        regra_queda.registrar(user_id, "push.entrega", {
            "episodeId": msg["episodeId"], "messageId": msg["messageId"],
            "messageType": msg["messageType"], "canal": canal.CANAL, "resultado": resultado})
        print(f"  [push] {msg['messageType']} user={user_id} canal={canal.CANAL} -> {resultado}")


def _emitir_pedido(user_id: int, pedido: "regra_queda.PedidoConfirmacao") -> None:
    msg = mensagens.criar_pedido_confirmacao(user_id, pedido.episode_id, pedido.timeout_s, pedido.motivo)
    regra_queda.anotar_mensagem(user_id, pedido.episode_id, msg["messageId"])
    _despachar(user_id, msg)


def _encerrar(enc: "regra_queda.Encerramento") -> None:
    _despachar(enc.user_id, mensagens.criar_encerramento(enc.user_id, enc.episode_id, enc.resultado))


def _avaliar_queda(user_id: int, source: str, payload: dict, alerta_imobilidade: dict | None) -> None:
    """Gatilho A (impacto + imobilidade) tem preferencia; gatilho B usa o alerta da regra de imobilidade."""
    pedido = regra_queda.avaliar(user_id, source, payload)
    if pedido is None and alerta_imobilidade is not None:
        pedido = regra_queda.abrir_por_imobilidade(user_id)
    if pedido is not None:
        _emitir_pedido(user_id, pedido)


def _vigia_loop() -> None:
    while True:
        time.sleep(const.INTERVALO_VIGIA_S)
        for enc in regra_queda.vigiar():
            _encerrar(enc)


def _decifrar_corpo():
    """Le o envelope do corpo. Retorna (dict) ou uma resposta de erro (tuple)."""
    envelope = request.get_json(silent=True)
    if not isinstance(envelope, dict) or "nonce" not in envelope or "ciphertext" not in envelope:
        return jsonify({"status": "erro", "mensagem": "Corpo invalido: esperado {'nonce': ..., 'ciphertext': ...}"}), 400
    try:
        corpo = json.loads(decrypt_payload(envelope["nonce"], envelope["ciphertext"]))
    except (DecryptionError, json.JSONDecodeError) as e:
        return jsonify({"status": "erro", "mensagem": f"Falha ao decifrar/parsear: {e}"}), 400
    if not isinstance(corpo, dict):   # lista, numero, texto, null: 400 em vez de AttributeError (R-09)
        return jsonify({"status": "erro", "mensagem": "Corpo decifrado invalido: esperado um objeto JSON"}), 400
    return corpo


def _ler_user_id(valor) -> int | None:
    """user_id aceito: inteiro (nao booleano) ou string de digitos. Qualquer outra coisa -> None (400).
    Evita que True vire 1 e que 1.9 vire 1 silenciosamente (R-09)."""
    if isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        return valor
    if isinstance(valor, str) and re.fullmatch(r"[0-9]+", valor.strip(), re.ASCII):
        return int(valor.strip())
    return None


def _resposta_cifrada(obj: dict):
    nonce, ct = encrypt_payload(json.dumps(obj))
    return jsonify({"nonce": nonce, "ciphertext": ct})


def verificar_versao(payload: dict, *, ausente: int | None = _SCHEMA_VERSION_AUSENTE_DADOS,
                     minima: int = 1) -> int | None:
    """Verifica `schemaVersion` (raiz do payload) ANTES de interpretar o resto (Marco 3, 10.2; DC-06/DC-07).

    Retorna a versao (int) quando suportada, ou None quando nao suportada. Nunca levanta excecao.
      - ausente: valor assumido se o campo nao existir; None = a ausencia e invalida.
      - minima:  menor versao aceita.
    Invalido: nao inteiro (inclui "3", 3.0, null), booleano (em Python True e int), menor que `minima`
    ou maior que mensagens.SCHEMA_VERSION."""
    if "schemaVersion" in payload:
        v = payload["schemaVersion"]
    else:
        v = ausente
    if v is None or isinstance(v, bool) or not isinstance(v, int):
        return None
    if v < minima or v > mensagens.SCHEMA_VERSION:
        return None
    return v


def _descartar_por_versao(rota: str, payload: dict):
    """Conteudo invalido de requisicao autenticada: loga e responde 200 com descartado (10.1)."""
    print(f"  [{rota}] descartado: schemaVersion nao suportado: {repr(payload.get('schemaVersion'))[:60]}")
    return jsonify({"status": "ok", "descartado": True, "motivo": "schemaVersion_nao_suportado"}), 200


def _versao_invalida_400():
    return jsonify({"status": "erro",
                    "mensagem": f"schemaVersion invalido ou nao suportado (maximo {mensagens.SCHEMA_VERSION})"}), 400


@app.route("/auth/login", methods=["POST"])
def login():
    corpo = _decifrar_corpo()
    if isinstance(corpo, tuple):
        return corpo
    if verificar_versao(corpo, ausente=mensagens.SCHEMA_VERSION) is None:   # ausente aceito (DC-07)
        return _versao_invalida_400()
    if not auth.device_id_valido(corpo.get("device_id")):
        return jsonify({"status": "erro", "mensagem": "device_id invalido"}), 400
    ident = str(corpo.get("identificador", ""))
    try:
        tokens = auth.login(ident, str(corpo.get("segredo", "")), corpo.get("fonte"), corpo.get("device_id"))
    except auth.Bloqueado:
        return negar(429, "bloqueado", ident)
    except auth.AuthError as e:
        return negar(401, e.codigo, ident)
    return _resposta_cifrada(tokens)


@app.route("/auth/refresh", methods=["POST"])
def refresh():
    corpo = _decifrar_corpo()
    if isinstance(corpo, tuple):
        return corpo
    if verificar_versao(corpo, ausente=mensagens.SCHEMA_VERSION) is None:   # ausente aceito (DC-07)
        return _versao_invalida_400()
    try:
        tokens = auth.refresh(str(corpo.get("refresh_token", "")))
    except auth.AuthError as e:
        return negar(401, e.codigo)
    return _resposta_cifrada(tokens)


@app.route("/eventos", methods=["POST"])
@exige("eventos:enviar")
def receber_evento():
    """Eventos discretos de retorno (push.ack, confirmacao.resposta). NAO passa pelo buffer de 1s."""
    corpo = _decifrar_corpo()
    if isinstance(corpo, tuple):
        return corpo
    if verificar_versao(corpo, ausente=mensagens.SCHEMA_VERSION) is None:   # ausente aceito (DC-07)
        return _versao_invalida_400()
    source = corpo.get("source")
    if not isinstance(source, str) or source not in const.FONTES_VALIDAS:
        return jsonify({
            "status": "erro",
            "mensagem": f"Campo 'source' ausente ou invalido (esperado 'app' ou 'wokwi'): {source}"
        }), 400
    user_id = _ler_user_id(corpo.get("user_id"))
    if user_id is None:
        return jsonify({"status": "erro", "mensagem": "user_id ausente ou nao-inteiro"}), 400
    try:
        exigir_escopo(g.principal, user_id, source)
    except ForaDeEscopo:
        return negar(403, "fora_de_escopo", g.principal.conta_id)
    # /eventos so existe a partir da v3: schemaVersion obrigatorio e >= 3 (DC-07)
    if verificar_versao(corpo, ausente=None, minima=3) is None:
        return _descartar_por_versao("eventos", corpo)
    try:
        ev = mensagens.validar_evento_retorno(corpo, source)
    except mensagens.EventoRetornoInvalido as e:
        print(f"  [eventos] descartado (invalido): {e}")
        return jsonify({"status": "ok", "descartado": True}), 200   # conteudo invalido: 200, como em /dados
    enc = regra_queda.registrar_retorno(user_id, ev)
    if enc:
        _encerrar(enc)
    return jsonify({"status": "ok"}), 200


@app.route("/dados", methods=["POST"])
@exige("dados:enviar")
def receber_dados():
    # 1. Decifrar o envelope (transporte) - mesmo mecanismo para app e wokwi. Garante que o corpo e um dict.
    payload = _decifrar_corpo()
    if isinstance(payload, tuple):
        return payload

    # 2. Identificar usuario (dono do dado) e fonte (app ou wokwi)
    source = payload.get("source")
    user_id_raw = payload.get("user_id")

    if not isinstance(source, str) or source not in const.FONTES_VALIDAS:
        return jsonify({
            "status": "erro",
            "mensagem": f"Campo 'source' ausente ou invalido (esperado 'app' ou 'wokwi'): {source}"
        }), 400

    user_id = _ler_user_id(user_id_raw)
    if user_id is None:
        return jsonify({
            "status": "erro",
            "mensagem": f"Campo 'user_id' ausente ou nao-inteiro: {user_id_raw}"
        }), 400

    # Estagio 2 de autorizacao: o user_id e a source declarados DEVEM ser os do token
    try:
        exigir_escopo(g.principal, user_id, source)
    except ForaDeEscopo:
        return negar(403, "fora_de_escopo", g.principal.conta_id)
    
    # schemaVersion (10.2 / DC-06): nao suportada -> descartada e logada, NAO entra no buffer nem nas regras.
    # Ausente vale como versao 1 (as regras avisam que esta desatualizada, como antes).
    versao = verificar_versao(payload)
    if versao is None:
        return _descartar_por_versao("dados", payload)

    # payload especifico da fonte, sem os campos de roteamento (ja estao no
    # nivel superior do registro gravado: user_id/timestamp)
    dados_fonte = {k: v for k, v in payload.items() if k not in ("source", "user_id")}

    # versao verificada acompanha o payload (uso da fusao por versao, R-07). Feito DEPOIS de montar
    # dados_fonte, para nao alterar o registro gravado no jsonl.
    payload["schemaVersion"] = versao

    device_id = str(payload.get("device_id") or payload.get("deviceId") or f"user{user_id}")

    print(f"[{_agora_iso()}] Dados decifrados: user_id={user_id} source={source} device_id={device_id}")

    # 3. Guarda no buffer de agregacao (vira 1 linha no jsonl do usuario no
    #    proximo flush de 1s, junto com a outra fonte, se houver)
    _registrar_no_buffer(user_id, source, dados_fonte)

    # 4. Fluxo priorizado (independente do buffer): regra de luz inadequada
    #    (app) ou validacao/estado/atuacao de imobilidade (wokwi)
    alerta_imobilidade = None
    alimenta_queda = True   # 9.1: validar -> deduplicar -> avaliar. Invalido ou duplicado nao chega a regra_queda
    if source == "app":
        # A analise de queda NAO depende do fluxo legado do app (R-06): se ele falhar, loga e segue.
        try:
            _processar_leitura_ambiente(payload, device_id)
        except Exception as e:
            _falha_fluxo_legado("leitura_ambiente", e)
        alimenta_queda = not regra_queda.eh_duplicado_app(user_id, device_id, payload)
    elif source == "wokwi":
        # Para o wokwi a queda depende so do estado ACEITO (R-03): excecao no fluxo legado = nao aceito.
        try:
            resultado = _processar_evento_wokwi(payload, device_id)
        except Exception as e:
            _falha_fluxo_legado("wokwi", e)
            resultado = ResultadoWokwi(WOKWI_INVALIDO)
        alerta_imobilidade = resultado.alerta
        alimenta_queda = resultado.estado == WOKWI_ACEITO

    # 5. Marco 3: analise de queda (acelerometro) -> se provavel, push de confirmacao
    if alimenta_queda:
        _avaliar_queda(user_id, source, payload, alerta_imobilidade)

    return jsonify({
        "status": "ok",
        "user_id": user_id,
        "source": source,
    }), 200


@app.route("/dados", methods=["GET"])
@exige("dados:ler")
def listar_dados():
    """Debug: lista os ultimos registros ja gravados (pos-flush)."""
    return jsonify(_registros_recentes)


@app.route("/estado/<device_id>", methods=["GET"])
@exige("estado:ler")
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
    auth.exigir_segredo()   # sem FLOW_JWT_SECRET o servidor nao sobe
    print(f"Servidor iniciando na porta {const.PORT}...")
    print(f"Salvando dados recebidos em: {os.path.abspath(const.DATA_DIR)} (1 arquivo .jsonl por usuario)")
    print(f"Janela de agregacao app+wokwi: {const.JANELA_AGREGACAO_S}s")
    print("Transporte: AES-256-GCM (chave compartilhada fixa, ver crypto_utils.py)")

    flush_thread = threading.Thread(target=_flush_loop, daemon=True)
    flush_thread.start()
    threading.Thread(target=_vigia_loop, daemon=True).start()   # encerra episodios vencidos

    # host="0.0.0.0" para aceitar conexoes de outros dispositivos na mesma rede
    app.run(host="0.0.0.0", port=const.PORT, threaded=True)   # threaded: uma thread por stream SSE
