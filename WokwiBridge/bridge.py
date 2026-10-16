"""
Gateway bidirecional entre a simulacao Wokwi (VS Code) e o servidor (Marco 3).

  Serial -> servidor : le JSON do ESP32, anexa user_id/source, cifra e envia
                       imobilidade.*                      -> POST /dados
                       push.ack / confirmacao.resposta    -> POST /eventos
  Servidor -> serial : mantem GET /push/wokwi (SSE) aberto, decifra, valida e traduz em comando de texto
                       CMD;BUZZER_ON;<messageId>;<timeoutMs>
                       CMD;BUZZER_OFF;<messageId>;<motivo>

O bridge NAO decide nada: decifra, valida, traduz, escreve.

Configuracao por variaveis de ambiente (nada de editar este arquivo):
  FLOW_BRIDGE_ID      conta do bridge (papel 'dispositivo')          [obrigatoria]
  FLOW_BRIDGE_SECRET  senha da conta                                   [obrigatoria]
  FLOW_SERVIDOR       URL base do servidor (padrao http://localhost:5000)
  FLOW_BRIDGE_DEVICE  deviceId do firmware que este bridge repassa (padrao esp32-decisao-imobilidade-01,
                      o DEVICE_ID de sketch.ino). O servidor exige que seja igual ao deviceId dos eventos.

Pre-requisitos:
  - Simulacao rodando no VS Code (F1 -> Wokwi: Start Simulator), com a aba do simulador VISIVEL
    (rfc2217ServerPort = 4000 em sketch/wokwi.toml).
  - Servidor (SensorServer/server.py) no ar e conta de papel 'dispositivo' (fonte 'wokwi') criada.

Uso:
  pip install -r requirements.txt
  python bridge.py
"""
import json
import os
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone

import requests
import serial

from crypto_utils import decrypt_payload, encrypt_payload

# --- Configuracoes ----------------------------------------------------------
RFC2217_URL = "rfc2217://localhost:4000"
BAUDRATE = 115200
SERVIDOR = os.environ.get("FLOW_SERVIDOR", "http://localhost:5000").rstrip("/")
BRIDGE_ID = os.environ.get("FLOW_BRIDGE_ID", "")
BRIDGE_SECRET = os.environ.get("FLOW_BRIDGE_SECRET", "")
# O login registra este device_id no token e o servidor confere com o deviceId de cada evento (403 se diferir):
# precisa ser o DEVICE_ID de sketch/sketch.ino.
BRIDGE_DEVICE = os.environ.get("FLOW_BRIDGE_DEVICE", "esp32-decisao-imobilidade-01")
RECONECTAR_A_CADA_S = 3
BACKOFF_MAX_S = 30
READ_TIMEOUT_SSE_S = 40          # > keepalive do servidor (15 s): detecta conexao morta
EVENTOS_RETORNO = {"push.ack", "confirmacao.resposta"}
MENSAGENS_VISTAS_MAX = 100
TIPO_SOLICITADA = "queda.confirmacao_solicitada"
TIPO_ENCERRADA = "queda.confirmacao_encerrada"
RESULTADOS_ENCERRAMENTO = {"CONFIRMADO", "EXPIRADO", "CANCELADO"}
# messageId vira campo da linha serial: so caracteres de UUID e no maximo 39 (o buffer do firmware tem 40 bytes,
# o UUID tem 36). Usado com fullmatch: '$' aceitaria uma quebra de linha no fim.
MESSAGE_ID_RE = re.compile(r"[A-Za-z0-9-]{1,39}")
# -----------------------------------------------------------------------------


def log(msg: str) -> None:
    print(f"[bridge] {msg}", flush=True)


def _cifrar(obj: dict) -> dict:
    nonce, ct = encrypt_payload(json.dumps(obj))
    return {"nonce": nonce, "ciphertext": ct}


def _decifrar(envelope: dict) -> dict:
    return json.loads(decrypt_payload(envelope["nonce"], envelope["ciphertext"]))


class Sessao:
    """Credenciais do bridge: login, renovacao e cabecalho Authorization."""

    def __init__(self):
        self._lock = threading.Lock()
        self.access = None
        self.refresh_token = None
        self.user_id = None

    def _aplicar(self, tokens: dict) -> None:
        self.access = tokens["access_token"]
        self.refresh_token = tokens["refresh_token"]
        self.user_id = tokens["user_ids"][0]       # o user_id vem do login, nao de configuracao manual

    def login(self) -> None:
        r = requests.post(f"{SERVIDOR}/auth/login", timeout=5, json=_cifrar({
            "schemaVersion": 3, "identificador": BRIDGE_ID, "segredo": BRIDGE_SECRET,
            "fonte": "wokwi", "device_id": BRIDGE_DEVICE}))
        if r.status_code != 200:
            raise RuntimeError(f"login recusado ({r.status_code}): {r.text}")
        self._aplicar(_decifrar(r.json()))

    def renovar(self, token_recusado: str | None = None) -> None:
        """refresh; se o refresh falhar, refaz o login com a senha do ambiente.

        token_recusado: o access token que acabou de receber 401. Se outra thread ja o trocou
        enquanto esta esperava o lock, nao ha o que renovar (o refresh token e de uso unico).
        """
        with self._lock:
            if token_recusado is not None and self.access != token_recusado:
                return
            try:
                r = requests.post(f"{SERVIDOR}/auth/refresh", timeout=5,
                                  json=_cifrar({"schemaVersion": 3, "refresh_token": self.refresh_token}))
                if r.status_code == 200:
                    self._aplicar(_decifrar(r.json()))
                    return
            except requests.RequestException:
                pass
            self.login()

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access}"}


class SerialHolder:
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
            except Exception as e:
                log(f"falha ao escrever na serial: {e}")
                return False


def conectar_serial():
    while True:
        try:
            ser = serial.serial_for_url(RFC2217_URL, baudrate=BAUDRATE, timeout=1)
            log(f"Conectado a porta serial do Wokwi ({RFC2217_URL})")
            return ser
        except Exception as e:
            log(f"Nao foi possivel conectar em {RFC2217_URL} ainda ({e}). "
                f"A simulacao ja esta rodando no VS Code? Tentando novamente em {RECONECTAR_A_CADA_S}s...")
            time.sleep(RECONECTAR_A_CADA_S)


# ------------------------------ serial -> servidor ---------------------------
def enviar(sessao: Sessao, rota: str, evento: dict) -> None:
    for tentativa in (1, 2):
        usado = sessao.access
        try:
            r = requests.post(f"{SERVIDOR}{rota}", json=_cifrar(evento), headers=sessao.headers(), timeout=5)
        except requests.RequestException as e:
            log(f"Falha ao enviar para {rota}: {e}")     # sem fila de retransmissao (decisao de arquitetura)
            return
        if r.status_code == 401 and tentativa == 1:
            try:
                sessao.renovar(usado)
            except Exception as e:                       # servidor fora / credencial recusada: descarta o evento
                log(f"Nao foi possivel renovar o token ({e}); evento descartado")
                return
            continue
        if r.status_code >= 300:
            log(f"Servidor respondeu {r.status_code} em {rota}: {r.text}")
        return


def processar_linha_serial(sessao: Sessao, linha: str) -> None:
    # O firmware imprime boas-vindas e mensagens de [DEBUG] que nao sao JSON: so repassamos eventos estruturados.
    if not linha.startswith("{"):
        log(f"(ignorado, nao e JSON) {linha}")
        return
    try:
        evento = json.loads(linha)
    except json.JSONDecodeError:
        log(f"(ignorado, JSON invalido) {linha}")
        return
    if not isinstance(evento, dict):
        log(f"(ignorado, JSON nao e objeto) {linha}")
        return
    evento["user_id"] = sessao.user_id
    evento["source"] = "wokwi"
    rota = "/eventos" if evento.get("eventType") in EVENTOS_RETORNO else "/dados"
    log(f"Evento capturado -> {rota}: {evento}")
    enviar(sessao, rota, evento)


def laco_serial(sessao: Sessao, holder: SerialHolder, abrir_serial=conectar_serial) -> None:
    holder.ser = abrir_serial()
    while True:
        try:
            bruta = holder.ser.readline()
        except Exception as e:
            log(f"Conexao serial perdida ({e}). Reconectando...")
            holder.ser = abrir_serial()
            continue
        if not bruta:
            continue                                     # timeout de leitura, tenta de novo
        linha = bruta.decode(errors="ignore").strip()
        if linha:
            processar_linha_serial(sessao, linha)


# ------------------------------ servidor -> serial ---------------------------
def _forma_invalida(msg: dict) -> str | None:
    """Motivo pelo qual a mensagem NAO pode virar linha serial, ou None se a forma esta correta.

    Protege o enquadramento da serial (CMD;...;<fim de linha>): um ';' ou quebra de linha no messageId, um timeoutMs
    que nao e inteiro ou um resultado fora do contrato nunca chegam ao firmware. Tipo desconhecido passa (tratar_push
    o ignora adiante, sem escrever nada).

    NAO filtra a FAIXA do timeoutMs: o firmware responde push.ack REJEITADO fora de 0 < t <= BUZZER_MAX_MS, e esse
    ack e a informacao util para o servidor.
    """
    mid = msg.get("messageId")
    if not isinstance(mid, str) or MESSAGE_ID_RE.fullmatch(mid) is None:
        return "messageId invalido"
    tipo = msg.get("messageType")
    if not isinstance(tipo, str):
        return "messageType ausente ou nao e texto"
    if tipo not in (TIPO_SOLICITADA, TIPO_ENCERRADA):
        return None
    payload = msg.get("payload")
    if not isinstance(payload, dict):
        return "payload ausente ou nao e objeto"
    if tipo == TIPO_SOLICITADA:
        t = payload.get("timeoutMs")
        if not isinstance(t, int) or isinstance(t, bool):       # bool e int em Python: True nao e prazo
            return "timeoutMs nao e inteiro"
    else:
        resultado = payload.get("resultado")
        if not isinstance(resultado, str) or resultado not in RESULTADOS_ENCERRAMENTO:
            return "resultado fora do contrato"
    return None


def traduzir(msg: dict) -> str | None:
    """Mensagem de push (v3) -> linha de comando para o firmware. None se o tipo nao e suportado.

    Espera uma mensagem que ja passou por _forma_invalida (tratar_push garante); sozinha, nao valida nada.
    """
    tipo, p = msg["messageType"], msg["payload"]
    if tipo == TIPO_SOLICITADA:
        return f"CMD;BUZZER_ON;{msg['messageId']};{int(p['timeoutMs'])}"
    if tipo == TIPO_ENCERRADA:
        return f"CMD;BUZZER_OFF;{msg['messageId']};{p['resultado']}"
    return None


def _expirada(msg: dict) -> bool:
    exp = datetime.strptime(msg["expiresAt"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= exp


def tratar_push(sessao: Sessao, holder: SerialHolder, vistas: deque, msg: dict) -> None:
    if msg.get("schemaVersion") != 3:
        log(f"push descartado: schemaVersion {msg.get('schemaVersion')}")
        return
    if msg.get("userId") != sessao.user_id:
        log("push descartado: userId diferente do deste bridge")
        return
    motivo = _forma_invalida(msg)
    if motivo is not None:
        log(f"push descartado: {motivo}")                    # sem eco do conteudo: o texto veio da rede
        return
    if msg["messageId"] in vistas:
        return                                           # idempotencia (reconexao pode reenviar)
    vistas.append(msg["messageId"])
    if _expirada(msg):
        log(f"push descartado: expirado ({msg['messageType']})")
        return
    cmd = traduzir(msg)
    if cmd is None:
        log(f"push ignorado: tipo nao suportado {msg['messageType']}")
        return
    ok = holder.escrever(cmd)
    log(f"push {msg['messageType']} -> serial {'OK' if ok else 'FALHOU'}: {cmd}")


def _ler_eventos(linhas):
    """Parser SSE puro: recebe as linhas (sem o fim de linha) e gera um dict por evento COMPLETO.

    Um evento termina na linha em branco; comentarios (':keepalive') sao ignorados; um evento que ficou sem a linha em
    branco final (conexao cortada no meio) nao e gerado.
    """
    ev: dict = {}
    for linha in linhas:
        if linha == "":                                  # fim de um evento SSE
            if ev:
                yield ev
            ev = {}
        elif not linha.startswith(":"):                  # ':' = comentario/keepalive
            k, _, v = linha.partition(":")
            ev[k] = v.strip()


def laco_push(sessao: Sessao, holder: SerialHolder) -> None:
    vistas: deque = deque(maxlen=MENSAGENS_VISTAS_MAX)
    last_id = 0
    espera = 1
    while True:
        try:
            h = {**sessao.headers(), "Accept": "text/event-stream"}
            usado = sessao.access
            if last_id:
                h["Last-Event-ID"] = str(last_id)
            with requests.get(f"{SERVIDOR}/push/wokwi", headers=h, stream=True,
                              timeout=(5, READ_TIMEOUT_SSE_S)) as r:
                if r.status_code == 401:
                    log("401 em /push/wokwi: renovando token")
                    sessao.renovar(usado)
                    time.sleep(1)                        # evita laco quente se o servidor continuar recusando
                    continue
                if r.status_code == 403:
                    log("403 em /push/wokwi: conta sem permissao. Nova tentativa em 30s.")
                    time.sleep(BACKOFF_MAX_S)
                    continue
                r.raise_for_status()
                log("Canal de push aberto (/push/wokwi)")
                espera = 1
                for ev in _ler_eventos(r.iter_lines(decode_unicode=True)):
                    if ev.get("event") == "push":
                        last_id = int(ev.get("id", last_id))
                        try:
                            msg = _decifrar(json.loads(ev["data"]))
                            tratar_push(sessao, holder, vistas, msg)
                        except Exception as e:           # uma mensagem ruim nao derruba o canal
                            log(f"push invalido descartado ({type(e).__name__}: {e})")
                    elif ev.get("event") == "reautenticar":
                        log("Servidor pediu reautenticacao; renovando token e reconectando")
                        sessao.renovar(usado)
                        break
        except requests.ConnectionError as e:
            # Servidor fora do ar. Zerar o Last-Event-ID e inofensivo (nao e necessario): o contador do servidor parte
            # do instante de subida e nunca fica atras do que o bridge guardou. O que o servidor reenviar cai em
            # 'vistas' (repetidas) ou nao e enviado (expiradas).
            last_id = 0
            log(f"Canal de push caiu ({e}). Reconectando em {espera}s...")
        except Exception as e:                           # timeout, HTTP != 2xx, falha de renovacao, etc.
            log(f"Canal de push caiu ({type(e).__name__}: {e}). Reconectando em {espera}s...")
        time.sleep(espera)
        espera = min(espera * 2, BACKOFF_MAX_S)


# ----------------------------------- main -----------------------------------
def main(abrir_serial=conectar_serial) -> None:
    if not BRIDGE_ID or not BRIDGE_SECRET:
        sys.exit("Defina FLOW_BRIDGE_ID e FLOW_BRIDGE_SECRET (ver INSTRUCOES.md).")
    sessao = Sessao()
    while True:
        try:
            sessao.login()
            break
        except Exception as e:
            log(f"Login falhou ({e}). Tentando novamente em {RECONECTAR_A_CADA_S}s...")
            time.sleep(RECONECTAR_A_CADA_S)
    log(f"Autenticado. user_id={sessao.user_id}")
    holder = SerialHolder()
    threading.Thread(target=laco_push, args=(sessao, holder), daemon=True).start()
    laco_serial(sessao, holder, abrir_serial)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Encerrado pelo usuario.")
        sys.exit(0)
