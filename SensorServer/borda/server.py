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

Alem disso, o fluxo priorizado (LeituraAmbiente -> regra de luz
inadequada -> alerta simulado para o cuidador) continua funcionando
normalmente para dados vindos do app, de forma independente do buffer acima
(processado assim que o payload chega, sem esperar o flush de 1s).

Autenticacao e autorizacao: nenhuma rota de dados e aberta. O cliente faz login em POST /auth/login
(identificador, segredo, fonte e device_id, cifrados com a mesma chave) e recebe, tambem cifrados, um
access token e um refresh token (auth.py). O refresh e de uso unico: POST /auth/refresh devolve um par
novo. Todas as demais rotas exigem "Authorization: Bearer <access_token>". Em cada uma, authz.py
checa a permissao do papel da conta (estagio 1, antes de decifrar o corpo) e, nas rotas com corpo, que o
user_id e a source declarados sao os do token (estagio 2). O que nao esta na politica e negado.

Caminho de volta (servidor -> dispositivo), tambem do Marco 3:

    1. Cada leitura aceita vira uma `nucleo.Leitura`, com o instante da ingestao, e o nucleo das regras (analise/nucleo.py:
       regra de luz, imobilidade e queda) a transforma em DECISOES, sem relogio, sem I/O e sem threads. Ao concluir
       QUEDA PROVAVEL (impacto seguido de imobilidade, ou imobilidade prolongada), uma das decisoes e um Comando
       de push, com a mensagem (comum/mensagens.py) ja montada.
    2. borda/caminho_direto.py e o ADAPTADOR do caminho direto (o unico ponto em que a borda chama a analise):
       analise/efeitos.py aplica as decisoes (arquivos, banco) e o adaptador despacha cada mensagem a
       TODOS os canais: GET /push/app (SSE para o app) e GET /push/wokwi (SSE para o bridge). Cada mensagem vai
       cifrada com a mesma chave e passa pelo hub de conexoes (sse_hub.py), que guarda as mensagens ainda validas
       para quem reconectar.
    3. O dispositivo responde com POST /eventos: "push.ack" (ativou ou rejeitou o push) e
       "confirmacao.resposta" (ESTOU_BEM ou SEM_RESPOSTA). O servidor valida o evento, associa a
       messageId ao episodio e, se for ESTOU_BEM de um episodio aberto, encerra o episodio.
    4. Uma thread vigia encerra por expiracao os episodios que ninguem respondeu. Todo encerramento
       (resposta ou expiracao) vira uma mensagem "queda.confirmacao_encerrada" enviada pelos canais.

Como rodar:
    pip install -r requirements.txt
    FLOW_JWT_SECRET=<segredo> python -m borda.server      (a partir de SensorServer/)
    (sem FLOW_JWT_SECRET o servidor nao sobe; contas se criam com "python -m borda.auth criar-conta")
    DOIS PROCESSOS (S5): "python -m borda" sobe a borda ISOLADA (le .env.borda, sem a analise) e "python -m analise" sobe
    a analise (ver borda/servico.py e analise/servico.py). "python -m borda.server" continua sendo o processo unico.
    Tambem vale "flask --app borda.server run" ou WSGI: o segredo e as threads de fundo sao tratados no import
    (FLOW_SEM_THREADS=1 desliga as threads, para os testes).
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from flask import Flask, request, jsonify, g

from borda import auth
from borda import canal_app
from borda import canal_wokwi
from borda import const as const_borda
from borda import saude
from borda import tempo
from borda import validacao
from comum import const as const_comum
from flow_db import repositorios
from borda.authz import exige, exigir_dispositivo, exigir_escopo, ForaDeEscopo, negar
from borda.crypto_utils import decrypt_payload, encrypt_payload, DecryptionError
from borda.sse_hub import HUB



def _pipeline():
    """O pipeline da borda (S5). Borda ISOLADA (`python -m borda`): `borda/via_banco.py`, sem nenhum import de `analise`.
    Processo unico: `borda/caminho_direto.py`, o UNICO ponto em que a borda chama a analise (importado so aqui, na hora
    do uso, para a borda isolada nunca carregar a analise). Os dois tem a mesma interface."""
    if const_borda.ISOLADA:
        from borda import via_banco
        return via_banco
    from borda import caminho_direto
    return caminho_direto


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = const_borda.MAX_CONTENT_LENGTH   # corpo maior: 413 (ver errorhandler abaixo)
app.register_blueprint(canal_app.bp)
app.register_blueprint(canal_wokwi.bp)

os.makedirs(const_comum.DATA_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Buffer de agregacao por usuario (protegido por lock, pois a thread de
# flush roda em paralelo com as requisicoes HTTP recebidas pelo Flask).
#
#   _buffer[user_id] = {"app": <payload ou None>, "wokwi": <payload ou None>}
# ---------------------------------------------------------------------------
_buffer_lock = threading.Lock()
_buffer: dict[int, dict[str, dict | None]] = {}

# Guarda os ultimos registros gravados (limite: const_borda.MAX_EM_MEMORIA), so para o GET /dados de debug.
_registros_recentes: list[dict] = []

# ---------------------------------------------------------------------------
# Banco (PostgreSQL). Cada leitura aceita entra numa FILA limitada em memoria e a thread de descarga a grava em
# lotes (tabela `leituras`). Nenhuma requisicao espera o banco: se ele cair, a fila segue enchendo ate o teto e as
# leituras mais antigas saem (descarte contado e logado). As demais escritas (alertas, episodios, comandos, entregas,
# respostas, confirmacoes, auditoria, estado) passam por repositorios.tentar_*: falha vai para o log, nunca vira 500.
# ---------------------------------------------------------------------------
_fila_lock = threading.Lock()
_parar_flush = threading.Event()          # S5: pede o fim da thread de descarga (encerrar())
_thread_flush: threading.Thread | None = None
_fila_leituras: list[dict] = []
_descartes_leituras = 0


def _instante_origem(source: str, dados: dict) -> datetime | None:
    """Relogio do dispositivo (so informativo). So o app manda epoch ms confiavel o bastante para registrar."""
    if source != "app":
        return None
    ms = dados.get("timestamp_envio")
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _enfileirar_leitura(user_id: int, source: str, device_id: str, dados: dict, versao: int, conta_id: str | None,
                        instante: datetime) -> None:
    """`instante` e o instante da ingestao (lido UMA vez em receber_dados): o mesmo que o nucleo das regras usa."""
    global _descartes_leituras
    if repositorios.borda() is None:
        return
    if source == "wokwi":
        tipo = dados.get("eventType")
    else:
        tipo = "leitura_ambiente" if "leitura_ambiente" in dados else "telemetria"
    leitura = {"instante_ingestao": instante, "user_id": user_id, "device_id": device_id,
               "fonte": source, "tipo_evento": tipo if isinstance(tipo, str) else None, "schema_version": versao,
               "instante_origem": _instante_origem(source, dados), "conta_id": conta_id, "payload": dados}
    with _fila_lock:
        _fila_leituras.append(leitura)
        excesso = len(_fila_leituras) - const_borda.MAX_FILA_LEITURAS
        if excesso > 0:
            del _fila_leituras[:excesso]
            _descartes_leituras += excesso
            print(f"  [db] fila de leituras cheia: {excesso} leitura(s) mais antiga(s) descartada(s) "
                  f"(total {_descartes_leituras})")


def _devolver_lote(lote: list[dict]) -> None:
    """Gravacao falhou: o lote volta para o INICIO da fila (ordem preservada), respeitando o teto."""
    global _descartes_leituras
    with _fila_lock:
        _fila_leituras[0:0] = lote
        excesso = len(_fila_leituras) - const_borda.MAX_FILA_LEITURAS
        if excesso > 0:
            del _fila_leituras[:excesso]
            _descartes_leituras += excesso


def _descarregar_leituras() -> bool:
    """Esvazia a fila em lotes. Chamada SO pela thread de descarga (uma unica escritora de `leituras`; no desligamento,
    por `encerrar()` depois de a thread parar). Devolve False se a gravacao falhou (o lote voltou para a fila)."""
    for _ in range(20):    # teto por passada: nao monopoliza a thread se a fila estiver enorme
        with _fila_lock:
            lote = _fila_leituras[:const_borda.TAMANHO_LOTE_LEITURAS]
            del _fila_leituras[:len(lote)]
        if not lote:
            return True
        if not repositorios.tentar_borda("leituras", lambda b: b.gravar_leituras(lote)):
            _devolver_lote(lote)
            return False
    return True


def _agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _arquivo_usuario(user_id: int) -> str:
    return os.path.join(const_comum.DATA_DIR, f"usuario_{user_id}.jsonl")


def _registrar_no_buffer(user_id: int, source: str, dados: dict) -> None:
    """Guarda a leitura mais recente de 'source' para 'user_id' na janela atual."""
    with _buffer_lock:
        slot = _buffer.setdefault(user_id, {"app": None, "wokwi": None})
        slot[source] = dados


def _gravar_usuario(user_id: int, fontes: dict) -> None:
    """Grava a linha de um usuario no flush. Pode levantar excecao (disco, permissao, dado nao serializavel)."""
    dado_app = fontes.get("app")
    dado_wokwi = fontes.get("wokwi")

    if dado_app is None and dado_wokwi is None:
        return  # nada chegou dessa vez para esse usuario

    registro = {
        "timestamp": _agora_iso(),
        "user_id": user_id,
        "app": dado_app,
        "wokwi": dado_wokwi,
    }

    filepath = _arquivo_usuario(user_id)
    if const_comum.GRAVAR_ARQUIVOS:
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(registro, ensure_ascii=False) + "\n")

    _registros_recentes.append(registro)
    if len(_registros_recentes) > const_borda.MAX_EM_MEMORIA:
        _registros_recentes.pop(0)

    if const_borda.GRAVAR_TELEMETRIA_LEGADO:   # dupla escrita: mesma linha, agora tambem na tabela `telemetria`
        repositorios.tentar_borda("telemetria", lambda b: b.gravar_telemetria([{
            "instante": datetime.fromisoformat(registro["timestamp"]), "user_id": user_id,
            "app": dado_app, "wokwi": dado_wokwi}]))

    print(
        f"[{registro['timestamp']}] usuario={user_id} "
        f"app={'ok' if dado_app else 'null'} "
        f"wokwi={'ok' if dado_wokwi else 'null'} -> {filepath}"
    )


def _flush_iteracao() -> None:
    """Uma passada do flush: esvazia o buffer e grava uma linha por usuario.
    Cada usuario tem a sua propria protecao: a falha de um nao impede a gravacao dos outros.
    A linha do usuario que falhou se perde (o buffer ja foi esvaziado); o erro fica no log."""
    with _buffer_lock:
        _buffer_local_copy = {uid: dict(v) for uid, v in _buffer.items()}
        _buffer.clear()

    for user_id, fontes in _buffer_local_copy.items():
        try:
            _gravar_usuario(user_id, fontes)
        except Exception as e:  # noqa: BLE001
            print(f"  [flush] falha ao gravar usuario={user_id} (linha perdida): {type(e).__name__}: {str(e)[:200]}")


def _flush_loop() -> None:
    """Roda em thread separada (a UNICA que grava `leituras`). A cada const_borda.INTERVALO_DESCARGA_S descarrega a fila de
    leituras em lote; a cada const_borda.JANELA_AGREGACAO_S grava tambem uma linha por usuario que recebeu dado
    (de app e/ou wokwi) na janela (telemetria agregada, como antes)."""
    ultima_janela = time.monotonic()
    while not _parar_flush.wait(const_borda.INTERVALO_DESCARGA_S):    # S5: o desligamento acorda e encerra a thread
        try:
            _descarregar_leituras()
        except Exception as e:  # noqa: BLE001  (a thread nunca pode morrer em silencio)
            print(f"  [flush] falha na descarga de leituras (a thread segue): {type(e).__name__}: {str(e)[:200]}")
        if time.monotonic() - ultima_janela >= const_borda.JANELA_AGREGACAO_S:
            ultima_janela = time.monotonic()
            try:
                _flush_iteracao()
            except Exception as e:  # noqa: BLE001
                print(f"  [flush] falha na iteracao (a thread segue): {type(e).__name__}: {str(e)[:200]}")


def _estatisticas_borda() -> dict:
    with _fila_lock:
        return {"fila_leituras": len(_fila_leituras), "leituras_descartadas": _descartes_leituras}


_monitor: "saude.MonitorSaude | None" = None


def _monitor_saude() -> "saude.MonitorSaude":
    """O monitor de saude do pipeline (S6), criado na primeira necessidade (a rota /health ou a subida das threads)."""
    global _monitor
    if _monitor is None:
        _monitor = saude.MonitorSaude(monitorada=lambda: _pipeline().analise_monitoravel(),
                                      estatisticas=_estatisticas_borda)
    return _monitor


@app.errorhandler(413)
def _corpo_grande(_erro):
    """Corpo acima de const_borda.MAX_CONTENT_LENGTH (o Flask levanta ao ler o corpo). Entra na auditoria como as demais negacoes."""
    return negar(413, "corpo_grande")


def _decifrar_corpo():
    """Le o envelope do corpo. Retorna (dict) ou uma resposta de erro (tuple)."""
    envelope = request.get_json(silent=True)
    try:
        validacao.validar_envelope(envelope)
    except validacao.CorpoInvalido as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 400
    try:
        corpo = json.loads(decrypt_payload(envelope["nonce"], envelope["ciphertext"]))
    except (DecryptionError, json.JSONDecodeError) as e:
        return jsonify({"status": "erro", "mensagem": f"Falha ao decifrar/parsear: {e}"}), 400
    if not isinstance(corpo, dict):   # lista, numero, texto, null: 400 em vez de AttributeError
        return jsonify({"status": "erro", "mensagem": "Corpo decifrado invalido: esperado um objeto JSON"}), 400
    return corpo


# A validacao estrutural vive em validacao.py (contratos em schemas/); estes nomes seguem valendo para as rotas e testes.
_ler_user_id = validacao.ler_user_id
versao_atual_da_fonte = validacao.versao_atual_da_fonte
verificar_versao = validacao.verificar_versao


def _resposta_cifrada(obj: dict):
    nonce, ct = encrypt_payload(json.dumps(obj))
    return jsonify({"nonce": nonce, "ciphertext": ct})


def _descartar_por_versao(rota: str, payload: dict):
    """Conteudo invalido de requisicao autenticada: loga e responde 200 com descartado (10.1)."""
    print(f"  [{rota}] descartado: schemaVersion nao suportado: {repr(payload.get('schemaVersion'))[:60]}")
    return jsonify({"status": "ok", "descartado": True, "motivo": "schemaVersion_nao_suportado"}), 200


def _versao_invalida_400(versao_atual: int):
    return jsonify({"status": "erro",
                    "mensagem": f"schemaVersion invalido ou nao suportado (maximo {versao_atual})"}), 400


@app.route("/auth/login", methods=["POST"])
def login():
    corpo = _decifrar_corpo()
    if isinstance(corpo, tuple):
        return corpo
    if verificar_versao(corpo, const_comum.SCHEMA_VERSION_AUTH) is None:   # ausente aceito
        return _versao_invalida_400(const_comum.SCHEMA_VERSION_AUTH)
    if not auth.device_id_valido(corpo.get("device_id")):
        return jsonify({"status": "erro", "mensagem": "device_id invalido"}), 400
    ident = str(corpo.get("identificador", ""))
    try:
        tokens = auth.login(ident, str(corpo.get("segredo", "")), corpo.get("fonte"), corpo.get("device_id"))
    except auth.Bloqueado as e:
        return negar(429, "bloqueado", ident if e.conta_existe else None)   # so conta que existe
    except auth.AuthError as e:
        return negar(401, e.codigo, ident if e.conta_existe else None)
    return _resposta_cifrada(tokens)


@app.route("/auth/refresh", methods=["POST"])
def refresh():
    corpo = _decifrar_corpo()
    if isinstance(corpo, tuple):
        return corpo
    if verificar_versao(corpo, const_comum.SCHEMA_VERSION_AUTH) is None:   # ausente aceito
        return _versao_invalida_400(const_comum.SCHEMA_VERSION_AUTH)
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
    try:
        source, user_id = validacao.validar_roteamento(corpo)
    except validacao.CorpoInvalido as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 400
    try:
        exigir_escopo(g.principal, user_id, source)
        exigir_dispositivo(g.principal, corpo)
    except ForaDeEscopo:
        return negar(403, "fora_de_escopo", g.principal.conta_id)
    # Mesma regra de /dados. Ausente ou versao antiga e aceita; maior que a atual ou invalida
    # e descartada e logada, com 200 (conteudo invalido de requisicao autenticada, 10.1).
    versao = verificar_versao(corpo, versao_atual_da_fonte(source))
    if versao is None:
        return _descartar_por_versao("eventos", corpo)
    try:
        ev = validacao.validar_evento_retorno(corpo, source)
    except validacao.EventoRetornoInvalido as e:
        print(f"  [eventos] descartado (invalido): {e}")
        return jsonify({"status": "ok", "descartado": True}), 200   # conteudo invalido: 200, como em /dados
    # Resposta crua do dispositivo -> banco (borda). Identidade SEMPRE do token, nunca do corpo.
    agora = tempo.agora_utc()   # instante da ingestao: o mesmo gravado em respostas_dispositivo e usado pelo nucleo
    repositorios.tentar_borda("resposta", lambda b: b.gravar_resposta({
        "instante_ingestao": agora, "user_id": user_id, "device_id": g.principal.device_id,
        "fonte": g.principal.fonte, "tipo": ev["eventType"], "message_id": ev.get("messageId"),
        "schema_version": versao, "payload": ev}))
    _pipeline().analisar_resposta(user_id, g.principal.device_id, g.principal.fonte, ev, versao, agora)
    return jsonify({"status": "ok"}), 200


@app.route("/dados", methods=["POST"])
@exige("dados:enviar")
def receber_dados():
    # 1. Decifrar o envelope (transporte) - mesmo mecanismo para app e wokwi. Garante que o corpo e um dict.
    payload = _decifrar_corpo()
    if isinstance(payload, tuple):
        return payload

    # 2. Identificar usuario (dono do dado) e fonte (app ou wokwi)
    try:
        source, user_id = validacao.validar_roteamento(payload)
    except validacao.CorpoInvalido as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 400

    # Estagio 2 de autorizacao: o user_id e a source declarados DEVEM ser os do token
    try:
        exigir_escopo(g.principal, user_id, source)
        exigir_dispositivo(g.principal, payload)
    except ForaDeEscopo:
        return negar(403, "fora_de_escopo", g.principal.conta_id)

    # schemaVersion: nao suportada -> descartada e logada, NAO entra no buffer nem nas regras.
    # Ausente vale como versao 1 (as regras avisam que esta desatualizada, como antes).
    versao = verificar_versao(payload, versao_atual_da_fonte(source))
    if versao is None:
        return _descartar_por_versao("dados", payload)

    # payload especifico da fonte, sem os campos de roteamento (ja estao no
    # nivel superior do registro gravado: user_id/timestamp)
    dados_fonte = {k: v for k, v in payload.items() if k not in ("source", "user_id")}

    # versao verificada acompanha o payload (uso da fusao por versao). Feito DEPOIS de montar
    # dados_fonte, para nao alterar o registro gravado no jsonl.
    payload["schemaVersion"] = versao

    if source == "app":   # contrato tolerante: so avisa (sem o conteudo); a telemetria segue como sempre
        erros_app = validacao.erros_telemetria_app(payload)
        if erros_app:
            print(f"  [dados] aviso: telemetria do app fora do contrato: {'; '.join(erros_app[:3])}")

    device_id = str(payload.get("device_id") or payload.get("deviceId") or f"user{user_id}")

    print(f"[{_agora_iso()}] Dados decifrados: user_id={user_id} source={source} device_id={device_id}")

    # 3. Guarda no buffer de agregacao (vira 1 linha no jsonl do usuario no
    #    proximo flush de 1s, junto com a outra fonte, se houver)
    agora = tempo.agora_utc()   # o UNICO relogio lido na requisicao: instante da ingestao (leituras.instante_ingestao)
    _registrar_no_buffer(user_id, source, dados_fonte)
    _enfileirar_leitura(user_id, source, device_id, dados_fonte, versao, g.principal.conta_id, agora)

    # 4. Analise (luz do app, imobilidade do wokwi, queda), pelo pipeline da borda (caminho direto ou, na borda isolada, nada: decide o outro processo). A telemetria ja foi
    #    aceita e enfileirada: falha ao decidir ou aplicar fica no log (la) e nao vira 500.
    _pipeline().analisar_leitura(user_id, device_id, source, dados_fonte, versao, agora)

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
    estado = _pipeline().estado_do_dispositivo(device_id)   # procura em todos os usuarios
    if estado is None:
        return jsonify({"status": "erro", "mensagem": f"device_id desconhecido: {device_id}"}), 404
    return jsonify(estado)


@app.route("/health", methods=["GET"])
def health():
    """Disponibilidade da borda (`status`, como sempre) e, desde a S6, o estado do pipeline: se a analise esta decidindo
    (batimento e atraso lidos do banco) e os contadores locais da fila de leituras. Sempre 200: a borda esta de pe mesmo
    com a analise parada; quem monitora olha `pipeline.estado` (borda/saude.py)."""
    return jsonify({"status": "ok", **_monitor_saude().corpo()})


@app.route("/", methods=["GET"])
def status():
    # Endpoint simples para o app/bridge checarem se o servidor esta de pe
    return jsonify({"status": "online"}), 200


# ---------------------------------------------------------------------------
# Inicializacao. Roda no import, para valer tambem em "flask --app borda.server run" e em servidores WSGI
# (o "flask" CLI usa o "app" do modulo): sem FLOW_JWT_SECRET valido a importacao falha, e as threads
# de flush e de vigia sobem. FLOW_SEM_THREADS=1 desliga as duas (usado pela suite de testes).
# ---------------------------------------------------------------------------
_threads_lock = threading.Lock()
_threads_iniciadas = False


def iniciar_threads() -> None:
    """Sobe as threads de fundo uma unica vez: flush do buffer e o pipeline (vigia, laco da analise e entrega no processo
    unico; so a entrega na borda isolada)."""
    global _threads_iniciadas, _thread_flush
    with _threads_lock:
        if _threads_iniciadas:
            return
        _parar_flush.clear()
        _thread_flush = threading.Thread(target=_flush_loop, name="flow-flush", daemon=True)
        _thread_flush.start()
        _pipeline().iniciar()
        if _pipeline().analise_monitoravel():
            _monitor_saude().iniciar()     # S6: alarmes em log (atraso, batimento velho, descartes); so com a analise pelo banco
        _threads_iniciadas = True


def _descarga_final(prazo_s: float) -> int:
    """Esvazia a fila de leituras no desligamento. Para ao esvaziar, ao falhar a gravacao ou ao estourar o prazo.
    Devolve quantas leituras ficaram sem gravar."""
    fim = time.monotonic() + prazo_s
    while time.monotonic() < fim:
        gravou = _descarregar_leituras()
        with _fila_lock:
            if not _fila_leituras:
                return 0
        if not gravou:
            break
    with _fila_lock:
        return len(_fila_leituras)


def encerrar(prazo_s: float | None = None) -> None:
    """DESLIGAMENTO ORDENADO da borda (S5). Chamado por borda/servico.py depois de parar de aceitar conexoes:
    1. para a thread de descarga (para a descarga final ter uma unica escritora) e o pipeline (entrega em curso termina);
    2. encerra os streams SSE (os clientes reconectam quando a borda voltar);
    3. descarrega o que restou da fila de leituras e do buffer de agregacao;
    4. fecha os pools do banco.
    Idempotente. Leitura que nao coube no prazo ou no banco fora do ar e contada no log."""
    global _threads_iniciadas, _thread_flush
    prazo = const_borda.PRAZO_ENCERRAMENTO_S if prazo_s is None else prazo_s
    with _threads_lock:
        _parar_flush.set()
        if _thread_flush is not None:
            _thread_flush.join(prazo)
        _thread_flush = None
        try:
            if _monitor is not None:
                _monitor.parar(prazo)
            _pipeline().parar(prazo)
        except Exception as e:  # noqa: BLE001  (o desligamento segue)
            print(f"  [borda] falha ao parar o pipeline (segue): {type(e).__name__}: {str(e)[:200]}")
        print(f"  [borda] encerrando: {HUB.fechar_todas()} stream(s) SSE fechado(s)")
        perdidas = _descarga_final(prazo)
        try:
            _flush_iteracao()
        except Exception as e:  # noqa: BLE001
            print(f"  [borda] falha na descarga final do buffer (segue): {type(e).__name__}: {str(e)[:200]}")
        if perdidas:
            print(f"  [borda] AVISO: {perdidas} leitura(s) nao gravada(s) no encerramento (banco fora do ar ou prazo)")
        repositorios.desinstalar()
        _threads_iniciadas = False
        print("  [borda] encerrada")


if os.environ.get("FLOW_SEM_THREADS") != "1":
    auth.exigir_segredo()   # sem FLOW_JWT_SECRET o servidor nao sobe
    if const_borda.ISOLADA:
        repositorios.iniciar_borda()   # S5: so o papel flow_borda; sem banco (ou esquema defasado) nao sobe
    else:
        repositorios.iniciar()  # sem banco (ou com esquema defasado) o servidor nao sobe; FLOW_DB_FAKE=1 usa o banco falso
    iniciar_threads()


if __name__ == "__main__":
    print(f"Servidor iniciando na porta {const_borda.PORT}...")
    print(f"Configuracao (.env): {const_comum.DOTENV_ARQUIVO} " + ("carregado" if const_comum.DOTENV_CARREGADO
          else "nao encontrado; usando so as variaveis de ambiente"))
    print(f"Banco: {'banco falso em memoria (FLOW_DB_FAKE=1)' if os.environ.get('FLOW_DB_FAKE') == '1' else 'PostgreSQL'}"
          f" | arquivos: {'sim' if const_comum.GRAVAR_ARQUIVOS else 'nao'}")
    print(f"Salvando dados recebidos em: {os.path.abspath(const_comum.DATA_DIR)} (1 arquivo .jsonl por usuario)")
    print(f"Janela de agregacao app+wokwi: {const_borda.JANELA_AGREGACAO_S}s")
    print("Transporte: AES-256-GCM (chave compartilhada fixa, ver crypto_utils.py)")

    # host="0.0.0.0" para aceitar conexoes de outros dispositivos na mesma rede
    app.run(host="0.0.0.0", port=const_borda.PORT, threaded=True)   # threaded: uma thread por stream SSE
