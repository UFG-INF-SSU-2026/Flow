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

    1. Cada leitura de aceleracao aceita alimenta regra_queda. Ao concluir QUEDA PROVAVEL (impacto seguido
       de imobilidade, ou imobilidade prolongada), regra_queda devolve um PedidoConfirmacao.
    2. server.py cria a mensagem (mensagens.py) e a despacha a TODOS os canais: GET /push/app (SSE para o
       app) e GET /push/wokwi (SSE para o bridge). Cada mensagem vai cifrada com a mesma chave e passa
       pelo hub de conexoes (sse_hub.py), que guarda as mensagens ainda validas para quem reconectar.
    3. O dispositivo responde com POST /eventos: "push.ack" (ativou ou rejeitou o push) e
       "confirmacao.resposta" (ESTOU_BEM ou SEM_RESPOSTA). O servidor valida o evento, associa a
       messageId ao episodio e, se for ESTOU_BEM de um episodio aberto, encerra o episodio.
    4. Uma thread vigia encerra por expiracao os episodios que ninguem respondeu. Todo encerramento
       (resposta ou expiracao) vira uma mensagem "queda.confirmacao_encerrada" enviada pelos canais.

Como rodar:
    pip install -r requirements.txt
    FLOW_JWT_SECRET=<segredo> python server.py
    (sem FLOW_JWT_SECRET o servidor nao sobe; contas se criam com "python auth.py criar-conta")
    Tambem vale "flask --app server run" ou WSGI: o segredo e as threads de fundo sao tratados no import
    (FLOW_SEM_THREADS=1 desliga as threads, para os testes).
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import NamedTuple
from flask import Flask, request, jsonify, g

import auth
import const   # PORT, DATA_DIR, JANELA_AGREGACAO_S, FONTES_VALIDAS... (ver const.py)
import mensagens
import regra_queda
import repositorios
import canal_app
import canal_wokwi
from authz import exige, exigir_dispositivo, exigir_escopo, ForaDeEscopo, negar
from crypto_utils import decrypt_payload, encrypt_payload, DecryptionError
from regra_luz import validar_evento, eh_duplicado, avaliar_regra, EventoInvalido
import regra_imobilidade

# Canais de push (servidor -> dispositivo). Todos expoem CANAL e enviar(user_id, mensagem).
CANAIS = [canal_app, canal_wokwi]

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = const.MAX_CONTENT_LENGTH   # corpo maior: 413 (ver errorhandler abaixo)
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

# ---------------------------------------------------------------------------
# Banco (PostgreSQL). Cada leitura aceita entra numa FILA limitada em memoria e a thread de descarga a grava em
# lotes (tabela `leituras`). Nenhuma requisicao espera o banco: se ele cair, a fila segue enchendo ate o teto e as
# leituras mais antigas saem (descarte contado e logado). As demais escritas (alertas, episodios, comandos, entregas,
# respostas, confirmacoes, auditoria, estado) passam por repositorios.tentar_*: falha vai para o log, nunca vira 500.
# ---------------------------------------------------------------------------
_fila_lock = threading.Lock()
_fila_leituras: list[dict] = []
_descartes_leituras = 0
_estado_gravado: dict[tuple, tuple] = {}   # (user_id, device_id) -> ultima "assinatura" do estado gravada no banco

_RESULTADO_ENTREGA = {"ENTREGUE_AO_CANAL": "enviado", "SEM_CANAL_ATIVO": "sem_conexao",
                      "TIPO_NAO_SUPORTADO": "nao_suportado", "ERRO_NO_CANAL": "erro"}


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


def _enfileirar_leitura(user_id: int, source: str, device_id: str, dados: dict, versao: int, conta_id: str | None) -> None:
    global _descartes_leituras
    if repositorios.borda() is None:
        return
    if source == "wokwi":
        tipo = dados.get("eventType")
    else:
        tipo = "leitura_ambiente" if "leitura_ambiente" in dados else "telemetria"
    leitura = {"instante_ingestao": datetime.now(timezone.utc), "user_id": user_id, "device_id": device_id,
               "fonte": source, "tipo_evento": tipo if isinstance(tipo, str) else None, "schema_version": versao,
               "instante_origem": _instante_origem(source, dados), "conta_id": conta_id, "payload": dados}
    with _fila_lock:
        _fila_leituras.append(leitura)
        excesso = len(_fila_leituras) - const.MAX_FILA_LEITURAS
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
        excesso = len(_fila_leituras) - const.MAX_FILA_LEITURAS
        if excesso > 0:
            del _fila_leituras[:excesso]
            _descartes_leituras += excesso


def _descarregar_leituras() -> None:
    """Esvazia a fila em lotes. Chamada SO pela thread de descarga (uma unica escritora de `leituras`)."""
    for _ in range(20):    # teto por passada: nao monopoliza a thread se a fila estiver enorme
        with _fila_lock:
            lote = _fila_leituras[:const.TAMANHO_LOTE_LEITURAS]
            del _fila_leituras[:len(lote)]
        if not lote:
            return
        if not repositorios.tentar_borda("leituras", lambda b: b.gravar_leituras(lote)):
            _devolver_lote(lote)
            return


def _registro_para_banco(user_id: int, tipo: str, linha: dict) -> None:
    """Ouvinte de regra_queda.registrar: o que antes so ia para confirmacoes_usuario_<id>.jsonl tambem vai ao banco.
    push.entrega -> entregas_push (borda); os demais (episodio.*, push.ack, confirmacao.resposta) -> confirmacoes (analise)."""
    instante = datetime.fromisoformat(linha["timestamp"])
    if tipo == "push.entrega":
        repositorios.tentar_borda("entrega", lambda b: b.gravar_entrega({
            "instante": instante, "comando_id": None, "message_id": linha.get("messageId"), "user_id": user_id,
            "device_id": None, "canal": linha.get("canal"),
            "resultado": _RESULTADO_ENTREGA.get(linha.get("resultado"), "erro"), "detalhe": linha.get("resultado")}))
        return
    repositorios.tentar_analise("confirmacao", lambda a: a.gravar_confirmacao({
        "instante": instante, "user_id": user_id, "tipo": tipo, "dados": linha,
        "episodio_id": linha.get("episodeId"), "message_id": linha.get("messageId"),
        "schema_version": const.SCHEMA_VERSION_MENSAGEM}))


regra_queda.OUVINTE_REGISTRO = _registro_para_banco


def _gravar_alerta_banco(user_id: int, device_id: str, tipo: str, alerta: dict, schema_version: int) -> None:
    repositorios.tentar_analise("alerta", lambda a: a.gravar_alerta({
        "instante": datetime.now(timezone.utc), "user_id": user_id, "device_id": device_id, "tipo": tipo,
        "mensagem": alerta.get("mensagem"), "dados": alerta, "schema_version": schema_version}))


def _gravar_pedido_banco(user_id: int, pedido: "regra_queda.PedidoConfirmacao", msg: dict, device_id: str | None) -> None:
    """Episodio (aguardando confirmacao) e o comando de push, na MESMA transacao."""
    agora = datetime.now(timezone.utc)
    expira = agora + timedelta(seconds=pedido.timeout_s + const.MARGEM_SERVIDOR_S)

    def grava(a):
        with a.transacao() as tx:
            tx.gravar_episodio({"episodio_id": pedido.episode_id, "user_id": user_id, "device_id": device_id,
                                "tipo": pedido.motivo, "estado": "aguardando_confirmacao", "aberto_em": agora,
                                "atualizado_em": agora, "encerrado_em": None, "motivo_encerramento": None,
                                "dados": {"timeout_s": pedido.timeout_s}})
            tx.gravar_comando({"criado_em": agora, "user_id": user_id, "device_id": None, "canal": "todos",
                               "message_id": msg["messageId"], "tipo": msg["messageType"], "payload": msg,
                               "expira_em": expira, "episodio_id": pedido.episode_id})
    repositorios.tentar_analise("pedido", grava)


def _gravar_encerramento_banco(enc: "regra_queda.Encerramento", msg: dict) -> None:
    agora = datetime.now(timezone.utc)

    def grava(a):
        with a.transacao() as tx:
            # Se o episodio nao chegou ao banco (queda na abertura), esta linha o cria com tipo DESCONHECIDO.
            tx.gravar_episodio({"episodio_id": enc.episode_id, "user_id": enc.user_id, "device_id": None,
                                "tipo": "DESCONHECIDO", "estado": "encerrado", "aberto_em": agora,
                                "atualizado_em": agora, "encerrado_em": agora, "motivo_encerramento": enc.resultado,
                                "dados": {}})
            tx.gravar_comando({"criado_em": agora, "user_id": enc.user_id, "device_id": None, "canal": "todos",
                               "message_id": msg["messageId"], "tipo": msg["messageType"], "payload": msg,
                               "expira_em": None, "episodio_id": enc.episode_id})
    repositorios.tentar_analise("encerramento", grava)


def _gravar_estado_banco(user_id: int, device_id: str) -> None:
    """Estado persistente da regra de imobilidade -> estado_dispositivo. So grava quando algo relevante mudou."""
    try:
        estado = regra_imobilidade.obter_estado(device_id)
        if estado is None or estado.get("user_id") != user_id:
            return
        chave = (user_id, device_id)
        assinatura = (estado.get("estado_atual"), estado.get("ultima_decisao"), estado.get("alerta_ativo"))
        if _estado_gravado.get(chave) == assinatura:
            return
        gravou = repositorios.tentar_analise("estado", lambda a: a.gravar_estado_dispositivo({
            "user_id": user_id, "device_id": device_id, "atualizado_em": datetime.now(timezone.utc),
            "versao_regra": estado.get("ultimo_schema_version") or 1, "estado": estado}))
        if gravou:
            _estado_gravado[chave] = assinatura
    except Exception as e:  # noqa: BLE001
        print(f"  [db] falha ao preparar estado_dispositivo user={user_id}: {type(e).__name__}: {str(e)[:200]}")


def _agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _nome_seguro(valor: str) -> str:
    """Valor vindo do cliente (device_id) para compor nome de arquivo: qualquer caractere fora de [A-Za-z0-9_.-]
    vira '_' (impede '/' e '..' de sair da pasta de dados) e o resultado e limitado a const.MAX_DEVICE_ID_LEN."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", valor)[:const.MAX_DEVICE_ID_LEN]


def _arquivo_usuario(user_id: int) -> str:
    return os.path.join(const.DATA_DIR, f"usuario_{user_id}.jsonl")


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
    if const.GRAVAR_ARQUIVOS:
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(registro, ensure_ascii=False) + "\n")

    _registros_recentes.append(registro)
    if len(_registros_recentes) > const.MAX_EM_MEMORIA:
        _registros_recentes.pop(0)

    if const.GRAVAR_TELEMETRIA_LEGADO:   # dupla escrita: mesma linha, agora tambem na tabela `telemetria`
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
    """Roda em thread separada (a UNICA que grava `leituras`). A cada const.INTERVALO_DESCARGA_S descarrega a fila de
    leituras em lote; a cada const.JANELA_AGREGACAO_S grava tambem uma linha por usuario que recebeu dado
    (de app e/ou wokwi) na janela (telemetria agregada, como antes)."""
    ultima_janela = time.monotonic()
    while True:
        time.sleep(const.INTERVALO_DESCARGA_S)
        try:
            _descarregar_leituras()
        except Exception as e:  # noqa: BLE001  (a thread nunca pode morrer em silencio)
            print(f"  [flush] falha na descarga de leituras (a thread segue): {type(e).__name__}: {str(e)[:200]}")
        if time.monotonic() - ultima_janela >= const.JANELA_AGREGACAO_S:
            ultima_janela = time.monotonic()
            try:
                _flush_iteracao()
            except Exception as e:  # noqa: BLE001
                print(f"  [flush] falha na iteracao (a thread segue): {type(e).__name__}: {str(e)[:200]}")


def _processar_leitura_ambiente(user_id: int, payload: dict, device_id: str) -> None:
    """Fluxo priorizado: LeituraAmbiente -> regra de luz -> alerta.
    So se aplica a payloads vindos do app (unico que emite esse evento)."""
    evento_ambiente = payload.get("leitura_ambiente")
    if evento_ambiente is None:
        return

    try:
        validar_evento(evento_ambiente)
    except EventoInvalido as e:
        print(f"  [leitura_ambiente] descartada (invalida): {e}")
        return

    if eh_duplicado(user_id, evento_ambiente):
        print(f"  [leitura_ambiente] descartada (duplicada, seq_num={evento_ambiente['seq_num']})")
        return

    resultado_regra = avaliar_regra(user_id, evento_ambiente)
    if resultado_regra is not None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"alerta_{_nome_seguro(device_id)}_{timestamp}.json"
        filepath = os.path.join(const.DATA_DIR, filename)
        if const.GRAVAR_ARQUIVOS:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(resultado_regra, f, ensure_ascii=False, indent=2)
        _gravar_alerta_banco(user_id, device_id, "luz_inadequada", resultado_regra, const.SCHEMA_VERSION_APP)
        print(f"  >>> ALERTA gerado: {filename} -> {resultado_regra['mensagem']}")


def _falha_fluxo_legado(nome: str, e: Exception) -> None:
    """Falha inesperada num fluxo legado (payload malformado ou bug): so loga. A resposta segue 200."""
    print(f"  [{nome}] falha no fluxo legado (ignorada): {type(e).__name__}: {str(e)[:200]}")


WOKWI_INVALIDO = "INVALIDO"
WOKWI_DUPLICADO = "DUPLICADO"
WOKWI_ACEITO = "ACEITO"


class ResultadoWokwi(NamedTuple):
    """Resultado de _processar_evento_wokwi. So ACEITO pode alimentar regra_queda (Marco 3, 9.1)."""
    estado: str                    # WOKWI_INVALIDO | WOKWI_DUPLICADO | WOKWI_ACEITO
    alerta: dict | None = None     # alerta de imobilidade (so quando ACEITO)


def _processar_evento_wokwi(user_id: int, payload: dict, device_id: str) -> ResultadoWokwi:
    """Cascata de validacao por eventType + tabela de estado persistente + atuacao para
    imobilidade.decisao com state=ALERTA_IMOBILIDADE. Espelha o fluxo
    priorizado de _processar_leitura_ambiente, mas para eventos do wokwi.

    Devolve o estado explicito (INVALIDO | DUPLICADO | ACEITO) para que o chamador nao confunda
    'descartado' com 'aceito, sem alerta'."""
    try:
        regra_imobilidade.validar_evento(payload)
    except regra_imobilidade.EventoInvalido as e:
        print(f"  [wokwi] evento descartado (invalido): {e}")
        return ResultadoWokwi(WOKWI_INVALIDO)

    if regra_imobilidade.eh_duplicado(user_id, payload):
        print(f"  [wokwi] evento descartado (duplicado/atrasado, sequence={payload.get('sequence')})")
        return ResultadoWokwi(WOKWI_DUPLICADO)

    alerta = regra_imobilidade.processar_evento(user_id, payload)
    if alerta is not None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"alerta_imobilidade_{_nome_seguro(device_id)}_{timestamp}.json"
        filepath = os.path.join(const.DATA_DIR, filename)
        if const.GRAVAR_ARQUIVOS:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(alerta, f, ensure_ascii=False, indent=2)
        _gravar_alerta_banco(user_id, device_id, "imobilidade", alerta, const.SCHEMA_VERSION_WOKWI)
        print(f"  >>> ALERTA gerado: {filename} -> {alerta['mensagem']}")

    return ResultadoWokwi(WOKWI_ACEITO, alerta)


# ---------------------------------------------------------------------------
# Marco 3: decisao de queda e push
# ---------------------------------------------------------------------------
RESULTADO_ERRO_NO_CANAL = "ERRO_NO_CANAL"   # canal.enviar levantou excecao (alem de ENTREGUE_AO_CANAL etc. dos canais)


def _despachar(user_id: int, msg: dict) -> None:
    """Entrega a mesma mensagem a TODOS os canais; registra o resultado por canal.
    Cada canal tem a sua propria protecao: a falha de um (envio ou gravacao do resultado) fica no log e
    nao impede os demais."""
    for canal in CANAIS:
        try:
            resultado = canal.enviar(user_id, msg)
        except Exception as e:  # noqa: BLE001
            resultado = RESULTADO_ERRO_NO_CANAL
            print(f"  [push] falha no canal={canal.CANAL} user={user_id}: {type(e).__name__}: {str(e)[:200]}")
        try:
            regra_queda.registrar(user_id, "push.entrega", {
                "episodeId": msg["episodeId"], "messageId": msg["messageId"],
                "messageType": msg["messageType"], "canal": canal.CANAL, "resultado": resultado})
        except Exception as e:  # noqa: BLE001
            print(f"  [push] falha ao registrar push.entrega canal={canal.CANAL} user={user_id}: "
                  f"{type(e).__name__}: {str(e)[:200]}")
        print(f"  [push] {msg['messageType']} user={user_id} canal={canal.CANAL} -> {resultado}")


def _emitir_pedido(user_id: int, pedido: "regra_queda.PedidoConfirmacao", device_id: str | None = None) -> None:
    msg = mensagens.criar_pedido_confirmacao(user_id, pedido.episode_id, pedido.timeout_s, pedido.motivo)
    regra_queda.anotar_mensagem(user_id, pedido.episode_id, msg["messageId"])
    _gravar_pedido_banco(user_id, pedido, msg, device_id)   # episodio + comando antes da entrega
    _despachar(user_id, msg)


def _encerrar(enc: "regra_queda.Encerramento") -> None:
    msg = mensagens.criar_encerramento(enc.user_id, enc.episode_id, enc.resultado)
    _gravar_encerramento_banco(enc, msg)
    _despachar(enc.user_id, msg)


def _avaliar_queda(user_id: int, source: str, payload: dict, alerta_imobilidade: dict | None,
                   device_id: str) -> None:
    """Gatilho A (impacto + imobilidade) tem preferencia; gatilho B usa o alerta da regra de imobilidade."""
    pedido = regra_queda.avaliar(user_id, source, payload, device_id=device_id)
    if pedido is None and alerta_imobilidade is not None:
        pedido = regra_queda.abrir_por_imobilidade(user_id)
    if pedido is not None:
        _emitir_pedido(user_id, pedido, device_id)


def _vigia_iteracao() -> None:
    """Uma passada do vigia: encerra os episodios vencidos e despacha o encerramento de cada um.
    Cada encerramento tem a sua propria protecao: a falha de um nao impede os seguintes.
    O episodio ja esta marcado como encerrado em regra_queda antes do despacho; se o despacho falhar,
    so o aviso ao dispositivo se perde e o erro fica no log."""
    for enc in regra_queda.vigiar():
        try:
            _encerrar(enc)
        except Exception as e:  # noqa: BLE001
            print(f"  [vigia] falha ao encerrar episodeId={enc.episode_id} user={enc.user_id}: "
                  f"{type(e).__name__}: {str(e)[:200]}")


def _vigia_loop() -> None:
    while True:
        time.sleep(const.INTERVALO_VIGIA_S)
        try:
            _vigia_iteracao()
        except Exception as e:  # noqa: BLE001  (a thread nunca pode morrer em silencio)
            print(f"  [vigia] falha na iteracao (a thread segue): {type(e).__name__}: {str(e)[:200]}")


@app.errorhandler(413)
def _corpo_grande(_erro):
    """Corpo acima de const.MAX_CONTENT_LENGTH (o Flask levanta ao ler o corpo). Entra na auditoria como as demais negacoes."""
    return negar(413, "corpo_grande")


def _decifrar_corpo():
    """Le o envelope do corpo. Retorna (dict) ou uma resposta de erro (tuple)."""
    envelope = request.get_json(silent=True)
    if not isinstance(envelope, dict) or "nonce" not in envelope or "ciphertext" not in envelope:
        return jsonify({"status": "erro", "mensagem": "Corpo invalido: esperado {'nonce': ..., 'ciphertext': ...}"}), 400
    try:
        corpo = json.loads(decrypt_payload(envelope["nonce"], envelope["ciphertext"]))
    except (DecryptionError, json.JSONDecodeError) as e:
        return jsonify({"status": "erro", "mensagem": f"Falha ao decifrar/parsear: {e}"}), 400
    if not isinstance(corpo, dict):   # lista, numero, texto, null: 400 em vez de AttributeError
        return jsonify({"status": "erro", "mensagem": "Corpo decifrado invalido: esperado um objeto JSON"}), 400
    return corpo


def _ler_user_id(valor) -> int | None:
    """user_id aceito: somente inteiro JSON (nao booleano). String (mesmo de digitos), decimal, null e
    ausente -> None (400). Evita que True vire 1, que 1.9 vire 1 e que "1" passe como 1 silenciosamente."""
    if isinstance(valor, int) and not isinstance(valor, bool):
        return valor
    return None


def _resposta_cifrada(obj: dict):
    nonce, ct = encrypt_payload(json.dumps(obj))
    return jsonify({"nonce": nonce, "ciphertext": ct})


def versao_atual_da_fonte(source: str) -> int:
    """Versao mais recente do contrato da fonte ('app' ou 'wokwi'), lida de const.py na hora da chamada."""
    if source == "app":
        return const.SCHEMA_VERSION_APP
    if source == "wokwi":
        return const.SCHEMA_VERSION_WOKWI
    raise ValueError(f"fonte desconhecida: {source!r}")


def verificar_versao(payload: dict, versao_atual: int) -> int | None:
    """Verifica `schemaVersion` (raiz do payload) ANTES de interpretar o resto.

    Funcao unica para todas as rotas, sem numero fixo: `versao_atual` e a do contrato da rota (quem chama
    escolhe: versao_atual_da_fonte(source) em /dados e /eventos, const.SCHEMA_VERSION_AUTH em login/refresh)
    e o piso vem de const.py. Retorna a versao (int) quando suportada, ou None. Nunca levanta excecao.
      - ausente: vale const.SCHEMA_VERSION_PADRAO;
      - de const.SCHEMA_VERSION_MINIMA ate `versao_atual`: aceita (inclusive versao menor que a atual);
      - invalido: nao inteiro (inclui "3", 3.0, null), booleano (em Python True e int), menor que a minima
        ou maior que `versao_atual`."""
    v = payload.get("schemaVersion", const.SCHEMA_VERSION_PADRAO)
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    if v < const.SCHEMA_VERSION_MINIMA or v > versao_atual:
        return None
    return v


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
    if verificar_versao(corpo, const.SCHEMA_VERSION_AUTH) is None:   # ausente aceito
        return _versao_invalida_400(const.SCHEMA_VERSION_AUTH)
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
    if verificar_versao(corpo, const.SCHEMA_VERSION_AUTH) is None:   # ausente aceito
        return _versao_invalida_400(const.SCHEMA_VERSION_AUTH)
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
        exigir_dispositivo(g.principal, corpo)
    except ForaDeEscopo:
        return negar(403, "fora_de_escopo", g.principal.conta_id)
    # Mesma regra de /dados. Ausente ou versao antiga e aceita; maior que a atual ou invalida
    # e descartada e logada, com 200 (conteudo invalido de requisicao autenticada, 10.1).
    versao = verificar_versao(corpo, versao_atual_da_fonte(source))
    if versao is None:
        return _descartar_por_versao("eventos", corpo)
    try:
        ev = mensagens.validar_evento_retorno(corpo, source)
    except mensagens.EventoRetornoInvalido as e:
        print(f"  [eventos] descartado (invalido): {e}")
        return jsonify({"status": "ok", "descartado": True}), 200   # conteudo invalido: 200, como em /dados
    # Resposta crua do dispositivo -> banco (borda). Identidade SEMPRE do token, nunca do corpo.
    repositorios.tentar_borda("resposta", lambda b: b.gravar_resposta({
        "instante_ingestao": datetime.now(timezone.utc), "user_id": user_id, "device_id": g.principal.device_id,
        "fonte": g.principal.fonte, "tipo": ev["eventType"], "message_id": ev.get("messageId"),
        "schema_version": versao, "payload": ev}))
    enc = regra_queda.registrar_retorno(user_id, ev)
    if enc:
        # O episodio ja esta encerrado e gravado; se o aviso aos dispositivos falhar, so ele se perde (log).
        try:
            _encerrar(enc)
        except Exception as e:  # noqa: BLE001
            print(f"  [eventos] falha ao avisar o encerramento episodeId={enc.episode_id} user={user_id}: "
                  f"{type(e).__name__}: {str(e)[:200]}")
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

    device_id = str(payload.get("device_id") or payload.get("deviceId") or f"user{user_id}")

    print(f"[{_agora_iso()}] Dados decifrados: user_id={user_id} source={source} device_id={device_id}")

    # 3. Guarda no buffer de agregacao (vira 1 linha no jsonl do usuario no
    #    proximo flush de 1s, junto com a outra fonte, se houver)
    _registrar_no_buffer(user_id, source, dados_fonte)
    _enfileirar_leitura(user_id, source, device_id, dados_fonte, versao, g.principal.conta_id)

    # 4. Fluxo priorizado (independente do buffer): regra de luz inadequada
    #    (app) ou validacao/estado/atuacao de imobilidade (wokwi)
    alerta_imobilidade = None
    alimenta_queda = True   # 9.1: validar -> deduplicar -> avaliar. Invalido ou duplicado nao chega a regra_queda
    if source == "app":
        # A analise de queda NAO depende do fluxo legado do app: se ele falhar, loga e segue.
        try:
            _processar_leitura_ambiente(user_id, payload, device_id)
        except Exception as e:
            _falha_fluxo_legado("leitura_ambiente", e)
        alimenta_queda = not regra_queda.eh_duplicado_app(user_id, device_id, payload)
    elif source == "wokwi":
        # Para o wokwi a queda depende so do estado ACEITO: excecao no fluxo legado = nao aceito.
        try:
            resultado = _processar_evento_wokwi(user_id, payload, device_id)
        except Exception as e:
            _falha_fluxo_legado("wokwi", e)
            resultado = ResultadoWokwi(WOKWI_INVALIDO)
        alerta_imobilidade = resultado.alerta
        alimenta_queda = resultado.estado == WOKWI_ACEITO
        if alimenta_queda:
            _gravar_estado_banco(user_id, device_id)

    # 5. Marco 3: analise de queda (acelerometro) -> se provavel, push de confirmacao
    # A telemetria ja foi aceita e gravada: falha ao avaliar/emitir o pedido fica no log e nao vira 500.
    if alimenta_queda:
        try:
            _avaliar_queda(user_id, source, payload, alerta_imobilidade, device_id)
        except Exception as e:  # noqa: BLE001
            print(f"  [queda] falha ao avaliar ou emitir o pedido user={user_id}: {type(e).__name__}: {str(e)[:200]}")

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
    estado = regra_imobilidade.obter_estado(device_id)   # procura em todos os usuarios
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


# ---------------------------------------------------------------------------
# Inicializacao. Roda no import, para valer tambem em "flask --app server run" e em servidores WSGI
# (o "flask" CLI usa o "app" do modulo): sem FLOW_JWT_SECRET valido a importacao falha, e as threads
# de flush e de vigia sobem. FLOW_SEM_THREADS=1 desliga as duas (usado pela suite de testes).
# ---------------------------------------------------------------------------
_threads_lock = threading.Lock()
_threads_iniciadas = False


def iniciar_threads() -> None:
    """Sobe as threads de fundo uma unica vez: flush do buffer e vigia de episodios vencidos."""
    global _threads_iniciadas
    with _threads_lock:
        if _threads_iniciadas:
            return
        threading.Thread(target=_flush_loop, name="flow-flush", daemon=True).start()
        threading.Thread(target=_vigia_loop, name="flow-vigia", daemon=True).start()   # encerra episodios vencidos
        _threads_iniciadas = True


if os.environ.get("FLOW_SEM_THREADS") != "1":
    auth.exigir_segredo()   # sem FLOW_JWT_SECRET o servidor nao sobe
    repositorios.iniciar()  # sem banco (ou com esquema defasado) o servidor nao sobe; FLOW_DB_FAKE=1 usa o banco falso
    iniciar_threads()


if __name__ == "__main__":
    print(f"Servidor iniciando na porta {const.PORT}...")
    print(f"Configuracao (.env): {const.DOTENV_ARQUIVO} " + ("carregado" if const.DOTENV_CARREGADO
          else "nao encontrado; usando so as variaveis de ambiente"))
    print(f"Banco: {'banco falso em memoria (FLOW_DB_FAKE=1)' if os.environ.get('FLOW_DB_FAKE') == '1' else 'PostgreSQL'}"
          f" | arquivos: {'sim' if const.GRAVAR_ARQUIVOS else 'nao'}")
    print(f"Salvando dados recebidos em: {os.path.abspath(const.DATA_DIR)} (1 arquivo .jsonl por usuario)")
    print(f"Janela de agregacao app+wokwi: {const.JANELA_AGREGACAO_S}s")
    print("Transporte: AES-256-GCM (chave compartilhada fixa, ver crypto_utils.py)")

    # host="0.0.0.0" para aceitar conexoes de outros dispositivos na mesma rede
    app.run(host="0.0.0.0", port=const.PORT, threaded=True)   # threaded: uma thread por stream SSE
