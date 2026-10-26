"""
BORDA (Servidor 1) do SensorServer: unico servico com porta de rede, com acesso ao banco, aos dispositivos, aos segredos de
transporte (chave AES, segredo JWT) e as contas. NAO decide nada: aceita, valida, grava, pergunta a analise e entrega.

Duas fontes de dados, que podem pertencer ao MESMO usuario (o mesmo idoso pode estar com o celular e com um dispositivo
wokwi ao mesmo tempo):

    App Android (SensorApp)   -> cifra o JSON (AES-256-GCM, ver crypto_utils.py) -> POST /dados {"nonce", "ciphertext"}
    Wokwi (via WokwiBridge)   -> o bridge le a serial do ESP32, anexa "user_id" e "source": "wokwi", cifra e -> POST /dados

Autenticacao e autorizacao: nenhuma rota de dados e aberta. O cliente faz login em POST /auth/login (identificador, segredo,
fonte e device_id, cifrados com a mesma chave) e recebe, tambem cifrados, um access token e um refresh token (auth.py). O
refresh e de uso unico: POST /auth/refresh devolve um par novo. Todas as demais rotas exigem "Authorization: Bearer <token>".
Em cada uma, authz.py checa a permissao do papel da conta (estagio 1, antes de decifrar o corpo) e, nas rotas com corpo,
que o user_id e a source declarados sao os do token (estagio 2). O que nao esta na politica e negado.

Caminho de uma leitura (a borda NAO decide; quem decide e a analise, `python -m analise`, outro processo):

    1. Cada leitura aceita vai para uma fila em memoria e, em lotes (a cada 250 ms), para a tabela `leituras`.
    2. Gravado o lote, a borda o manda a analise (borda/cliente_analise.py: POST /analisar). A analise responde com
       DECISOES (comandos de push, alertas, registros de confirmacao, estado do dispositivo).
    3. A borda grava as decisoes numa transacao (borda/efeitos.py) e, so depois do commit, entrega o push pelo SSE:
       GET /push/app (app) e GET /push/wokwi (bridge), cifrado e com replay para quem reconectar (sse_hub.py).
    4. O dispositivo responde com POST /eventos ("push.ack", "confirmacao.resposta"): a borda valida e manda a resposta a
       analise pelo mesmo caminho, com a identidade vinda do TOKEN (nunca do corpo).
    5. Uma thread de vigia chama POST /vigiar da analise para encerrar por expiracao os episodios que ninguem respondeu.
    6. GET /estado/<device_id> le o resumo gravado em `estado_dispositivo`; GET /health mostra se a analise responde.

Se a analise cai, a borda segue aceitando e gravando leituras (a fila de espera e limitada) e o /health acusa "degradado"; ao
voltar (ou reiniciar), a analise recebe o estado de volta (/restaurar) e a fila. Se o banco cai, as leituras ficam na fila
limitada e nenhuma decisao e aplicada nem push enviado ate o banco voltar.

Como rodar (a partir de SensorServer/; scripts/ faz tudo na ordem certa):
    python -m borda      sobe a borda (contas se criam com "python -m borda.auth criar-conta")
    python -m analise    sobe a analise (so escuta em 127.0.0.1)
    "python -m borda.server" (ou "flask --app borda.server run --cert certs/borda.crt --key certs/borda.key", ou WSGI com TLS
    na frente) sobe a MESMA borda sem desligamento ordenado: serve ao desenvolvimento. Sem a analise rodando ninguem decide
    queda, imobilidade nem luz. Toda ponta fala TLS: o modo de desenvolvimento tambem serve HTTPS.
    Sem FLOW_JWT_SECRET o servidor nao sobe. O segredo, o banco e as threads de fundo sao tratados no import
    (FLOW_SEM_THREADS=1 desliga as threads, para os testes).
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify, g

from borda import auth
from borda import canal_app
from borda import canal_wokwi
from borda import cliente_analise
from borda import const as const_borda
from borda import efeitos
from borda import tempo
from borda import validacao
from comum import const as const_comum
from comum import tls
from comum.contrato_analise import Leitura, Resposta, instante_para_json
from flow_db import repositorios
from borda.authz import exige, exigir_dispositivo, exigir_escopo, ForaDeEscopo, negar
from borda.crypto_utils import decrypt_payload, encrypt_payload, DecryptionError
from borda.sse_hub import HUB


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = const_borda.MAX_CONTENT_LENGTH   # corpo maior: 413 (ver errorhandler abaixo)
app.register_blueprint(canal_app.bp)
app.register_blueprint(canal_wokwi.bp)

# ---------------------------------------------------------------------------
# Filas em memoria (limitadas). Nenhuma requisicao espera o banco nem a analise.
#   _fila_leituras  leituras aceitas que ainda nao foram gravadas em `leituras` (a thread de fundo as grava em lote)
#   _fila_analise   itens (leituras ja gravadas e respostas dos dispositivos) que esperam a analise
# Se o banco ou a analise cair, as filas enchem ate o teto e os itens mais antigos saem (descarte contado e logado).
# ---------------------------------------------------------------------------
_fila_lock = threading.Lock()
_parar_flush = threading.Event()          # pede o fim das threads de fundo (encerrar())
_thread_flush: threading.Thread | None = None
_thread_vigia: threading.Thread | None = None
_thread_retencao: threading.Thread | None = None
_fila_leituras: list[dict] = []
_fila_analise: list = []
_descartes_leituras = 0
_descartes_analise = 0
_localizacao_rejeitadas = 0               # blocos `localizacao` recusados desde que a borda subiu (/health)
_ultimo_log: dict[str, float] = {}        # chave da falha -> instante (monotonic) da ultima linha de log escrita
_cliente: "cliente_analise.ClienteAnalise | None" = None
_aplicador = efeitos.Aplicador()


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


def _log_falha(chave: str, mensagem: str, prefixo: str = "pipeline") -> None:
    """Falha que se repete a cada passo (banco ou analise fora do ar, fila cheia, passo que levanta): no maximo uma linha de
    log por INTERVALO_LOG_FALHA_S **para cada `chave`**, assim uma falha nao esconde a outra."""
    agora = time.monotonic()
    if agora - _ultimo_log.get(chave, float("-inf")) >= const_borda.INTERVALO_LOG_FALHA_S:
        _ultimo_log[chave] = agora
        print(f"  [{prefixo}] {mensagem}")


def _enfileirar_leitura(user_id: int, source: str, device_id: str, dados: dict, versao: int, conta_id: str | None,
                        instante: datetime) -> None:
    """`instante` e o instante da ingestao (lido UMA vez em receber_dados): o mesmo que as regras usam."""
    global _descartes_leituras
    if repositorios.repo() is None:
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
            _log_falha("fila_leituras", f"fila de leituras cheia: leitura(s) mais antiga(s) descartada(s) "
                                        f"(total {_descartes_leituras})")


def _entrar_na_fila_analise(itens: list, no_inicio: bool = False) -> None:
    """Poe itens na fila da analise (no inicio, se estao sendo devolvidos), respeitando o teto."""
    global _descartes_analise
    with _fila_lock:
        if no_inicio:
            _fila_analise[0:0] = itens
        else:
            _fila_analise.extend(itens)
        excesso = len(_fila_analise) - const_borda.MAX_FILA_ANALISE
        if excesso > 0:
            del _fila_analise[:excesso]
            _descartes_analise += excesso
            _log_falha("fila_analise", f"fila de espera da analise cheia: item(ns) mais antigo(s) descartado(s) "
                                       f"(total {_descartes_analise})")


def _devolver_lote(lote: list[dict]) -> None:
    """Gravacao falhou: o lote volta para o INICIO da fila (ordem preservada), respeitando o teto."""
    global _descartes_leituras
    with _fila_lock:
        _fila_leituras[0:0] = lote
        excesso = len(_fila_leituras) - const_borda.MAX_FILA_LEITURAS
        if excesso > 0:
            del _fila_leituras[:excesso]
            _descartes_leituras += excesso
            _log_falha("fila_leituras", f"fila de leituras cheia: leitura(s) mais antiga(s) descartada(s) "
                                        f"(total {_descartes_leituras})")


def _leitura_de_linha(linha: dict) -> Leitura:
    """A linha gravada vira a entrada da analise (e da restauracao). A localizacao fica de fora: nenhuma regra a usa, e
    dado sensivel nao precisa sair da borda."""
    payload = linha["payload"]
    if "localizacao" in payload:
        payload = {k: v for k, v in payload.items() if k != "localizacao"}
    return Leitura(linha["instante_ingestao"], linha["user_id"], linha["device_id"], linha["fonte"], payload,
                   linha["schema_version"])


def _descarregar_leituras() -> bool:
    """Esvazia a fila de leituras em lotes: grava no banco e passa cada lote gravado para a fila da analise. Chamada SO
    pela thread de fundo (uma unica escritora de `leituras`; no desligamento, por `encerrar()` depois de a thread parar).
    Devolve False se a gravacao falhou (o lote voltou para a fila)."""
    for _ in range(20):    # teto por passada: nao monopoliza a thread se a fila estiver enorme
        with _fila_lock:
            lote = _fila_leituras[:const_borda.TAMANHO_LOTE_LEITURAS]
            del _fila_leituras[:len(lote)]
        if not lote:
            return True
        try:
            repo = repositorios.repo()
            if repo is None:
                raise RuntimeError("sem banco")
            repo.gravar_leituras(lote)
        except Exception as e:  # noqa: BLE001  (o banco nunca derruba a borda)
            _devolver_lote(lote)
            _log_falha("banco", f"banco indisponivel: {len(lote)}+ leitura(s) esperam na fila: "
                                f"{type(e).__name__}: {str(e)[:200]}")
            return False
        _entrar_na_fila_analise([_leitura_de_linha(l) for l in lote])
    return True


def _montar_restauracao() -> dict:
    """O que a analise precisa para reconstruir o estado (POST /restaurar): as leituras dos ultimos segundos, os episodios
    abertos e recentes e as mensagens deles, tudo do banco. Os itens ainda na fila da analise ficam de fora das leituras
    (eles serao enviados de qualquer jeito). Levanta se o banco falhar."""
    repo = repositorios.repo()
    if repo is None:
        raise RuntimeError("sem banco")
    agora = tempo.agora_utc()
    with _fila_lock:
        antes_de = min((i.instante for i in _fila_analise), default=None)
    linhas = repo.ler_leituras_recentes(agora - timedelta(seconds=const_borda.JANELA_RESTAURACAO_S), antes_de,
                                        const_borda.LIMITE_RESTAURACAO)
    episodios = repo.listar_episodios_recentes(agora - timedelta(seconds=const_borda.RETENCAO_EPISODIOS_S))
    comandos = repo.listar_comandos_de_episodios([e["episodio_id"] for e in episodios])
    leituras = []
    for linha in linhas:
        try:
            leituras.append(_leitura_de_linha(linha).para_json())
        except (KeyError, ValueError):
            continue
    return {"leituras": leituras,
            "episodios": [{**e, **{k: instante_para_json(e.get(k)) for k in ("aberto_em", "atualizado_em", "encerrado_em")}}
                          for e in episodios],
            "mensagens": [{"episodio_id": c["episodio_id"], "message_id": c["message_id"], "tipo": c["tipo"]}
                          for c in comandos]}


def configurar_analise(transporte) -> "cliente_analise.ClienteAnalise":
    """Liga a borda a analise por `transporte` (HTTP em producao; chamada direta nos testes)."""
    global _cliente
    _cliente = cliente_analise.ClienteAnalise(transporte, _montar_restauracao)
    return _cliente


def _analisar_fila() -> bool:
    """Manda a fila de espera a analise em lotes e aplica as decisoes (borda/efeitos.py). Chamada pela thread de fundo.
    Devolve False se a analise ou o banco falharam (o lote fica na fila; as decisoes ja tomadas ficam guardadas)."""
    if _cliente is None:
        return True
    if not _aplicador.aplicar():                   # decisoes de antes que ainda nao chegaram ao banco
        return False
    for _ in range(20):
        with _fila_lock:
            lote = _fila_analise[:const_borda.TAMANHO_LOTE_LEITURAS]
        if not lote:
            return True
        try:
            decisoes = _cliente.analisar(lote)
        except cliente_analise.AnaliseIndisponivel as e:
            _log_falha("analise", f"analise fora do ar ({e}): {len(lote)}+ item(ns) esperam na fila")
            return False
        except cliente_analise.RequisicaoRecusada:
            decisoes = None
            _log_falha("analise_recusou", f"lote de {len(lote)} item(ns) recusado pela analise: descartado")
        with _fila_lock:
            del _fila_analise[:len(lote)]
        if decisoes is not None and not _aplicador.aplicar(decisoes):
            return False
    return True


def ciclo_pipeline() -> None:
    """Um passo da thread de fundo: grava as leituras e passa pela analise. Nao levanta."""
    try:
        _descarregar_leituras()
        _analisar_fila()
    except Exception as e:  # noqa: BLE001  (a thread nunca pode morrer em silencio)
        _log_falha("passo", f"falha no passo (a thread segue): {type(e).__name__}: {str(e)[:200]}")


def passo_vigia() -> None:
    """Encerra por expiracao os episodios vencidos (a analise decide; a borda grava e entrega). Usa o relogio real MENOS
    ATRASO_VIGIA_S, para o dado ainda em transito ser analisado antes. Nao levanta."""
    if _cliente is None:
        return
    try:
        instante = tempo.agora_utc() - timedelta(seconds=const_borda.ATRASO_VIGIA_S)
        decisoes = _cliente.vigiar(instante)
    except cliente_analise.AnaliseIndisponivel as e:
        _log_falha("analise", f"analise fora do ar ({e}): o vigia nao encerra episodios ate ela voltar")
        return
    except Exception as e:  # noqa: BLE001
        _log_falha("vigia", f"falha no vigia (a thread segue): {type(e).__name__}: {str(e)[:200]}")
        return
    _aplicador.aplicar(decisoes)


def passo_retencao() -> None:
    """Retencao das tabelas comuns (alertas, confirmacoes, episodios encerrados, comandos_push): apaga o que passou de
    RETENCAO_HISTORICO_DIAS, com o papel da borda (que ja tem DELETE). `leituras` fica por conta da politica do TimescaleDB.
    Uma vez por hora (e ao subir). Nao levanta: banco fora do ar so vira uma linha de log e tenta de novo na proxima."""
    try:
        repo = repositorios.repo()
        if repo is None:
            return
        apagadas = repo.podar_historico(tempo.agora_utc() - timedelta(days=const_borda.RETENCAO_HISTORICO_DIAS))
    except Exception as e:  # noqa: BLE001  (o banco nunca derruba a borda)
        _log_falha("retencao", f"retencao nao executada (tenta de novo na proxima hora): {type(e).__name__}: {str(e)[:200]}",
                   prefixo="retencao")
        return
    if any(apagadas.values()):
        print(f"  [retencao] apagadas (mais de {const_borda.RETENCAO_HISTORICO_DIAS} dias): "
              + ", ".join(f"{t}={n}" for t, n in apagadas.items() if n))


def _agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loop(passo, intervalo_s: float, ao_iniciar: bool = False) -> None:
    if ao_iniciar and not _parar_flush.is_set():
        passo()
    while not _parar_flush.wait(intervalo_s):      # o desligamento acorda e encerra a thread
        passo()


def _estatisticas_borda() -> dict:
    with _fila_lock:
        return {"fila_leituras": len(_fila_leituras), "leituras_descartadas": _descartes_leituras,
                "localizacao_rejeitadas": _localizacao_rejeitadas}


@app.errorhandler(413)
def _corpo_grande(_erro):
    """Corpo acima de const_borda.MAX_CONTENT_LENGTH (o Flask levanta ao ler o corpo). Entra na auditoria como as demais negacoes."""
    return negar(413, "corpo_grande")


def _filtrar_localizacao(source: str, dados_fonte: dict) -> None:
    """Valida o bloco `localizacao` (so o app o envia) pelo schema, SEMPRE, sem confiar na filtragem do app. Bloco invalido,
    ou vindo de outra fonte, e retirado de `dados_fonte` (nao e gravado) e o resto da telemetria segue. O aviso diz so
    QUAIS campos falharam (nunca latitude ou longitude), com limite de repeticao, e o contador sobe (/health)."""
    global _localizacao_rejeitadas
    if "localizacao" not in dados_fonte:
        return
    if source != "app":
        campos = ["fonte"]
    else:
        campos = validacao.campos_com_erro(validacao.erros_localizacao(dados_fonte["localizacao"]))
        if not campos:
            return
    del dados_fonte["localizacao"]
    with _fila_lock:
        _localizacao_rejeitadas += 1
    _log_falha("localizacao", f"localizacao rejeitada: {', '.join(campos)}", prefixo="dados")


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


def _login_sem_banco(erro: "auth.ServicoIndisponivel"):
    """Contas e sessoes estao no banco: sem ele, login e refresh respondem 503. Quem ja tem access token segue enviando
    (o JWT e conferido sem banco) e as leituras continuam na fila."""
    return negar(503, erro.codigo, mensagem="banco indisponivel; tente de novo em instantes")


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
    except auth.ServicoIndisponivel as e:
        return _login_sem_banco(e)
    except auth.DeviceIdObrigatorio as e:
        return negar(400, e.codigo, ident, mensagem="device_id obrigatorio para esta conta")
    except auth.DeviceNaoPermitido as e:
        return negar(403, e.codigo, ident)
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
    except auth.ServicoIndisponivel as e:
        return _login_sem_banco(e)
    except auth.AuthError as e:
        return negar(401, e.codigo)
    return _resposta_cifrada(tokens)


@app.route("/eventos", methods=["POST"])
@exige("eventos:enviar")
def receber_evento():
    """Eventos discretos de retorno (push.ack, confirmacao.resposta): vao a analise pela fila, como as leituras."""
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
    # Resposta do dispositivo -> fila da analise. Identidade SEMPRE do token, nunca do corpo.
    agora = tempo.agora_utc()   # instante da ingestao: a analise decide por ele
    _entrar_na_fila_analise([Resposta(agora, user_id, g.principal.device_id, g.principal.fonte, ev, versao)])
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

    # payload especifico da fonte, sem os campos de roteamento (a coluna user_id e a fonte da leitura os guardam)
    dados_fonte = {k: v for k, v in payload.items() if k not in ("source", "user_id")}

    # localizacao: dado sensivel, validado aqui; o bloco invalido sai de dados_fonte antes de ir a fila (nao e gravado)
    _filtrar_localizacao(source, dados_fonte)

    # versao verificada acompanha o payload (a validacao do contrato a enxerga). Feito DEPOIS de montar
    # dados_fonte, para nao alterar a leitura gravada em `leituras`.
    payload["schemaVersion"] = versao

    if source == "app":   # contrato tolerante: so avisa (sem o conteudo); a telemetria segue como sempre
        erros_app = validacao.erros_telemetria_app(payload)
        if erros_app:
            print(f"  [dados] aviso: telemetria do app fora do contrato: {'; '.join(erros_app[:3])}")

    # O device_id gravado e SEMPRE o do token (o informado no login e conferido pela conta); o do payload so pode coincidir.
    device_id = g.principal.device_id
    if not device_id:      # idoso e dispositivo sempre o tem; defesa para um token sem ele
        return negar(403, "fora_de_escopo", g.principal.conta_id)

    print(f"[{_agora_iso()}] Dados decifrados: user_id={user_id} source={source} device_id={device_id}")

    # 3. Enfileira a leitura (a thread de fundo a grava em `leituras` e a manda a analise)
    agora = tempo.agora_utc()   # o UNICO relogio lido na requisicao: instante da ingestao (leituras.instante_ingestao), que a analise usa
    _enfileirar_leitura(user_id, source, device_id, dados_fonte, versao, g.principal.conta_id, agora)

    # 4. Fim da borda: a leitura segue para a tabela `leituras` (descarga em lote) e quem decide luz, imobilidade e queda e a
    #    analise, em outro processo. Nada que ela faca (ou deixe de fazer) muda a resposta desta rota.

    return jsonify({
        "status": "ok",
        "user_id": user_id,
        "source": source,
    }), 200


@app.route("/dados", methods=["GET"])
@exige("dados:ler")
def listar_dados():
    """Debug (so admin): as ultimas leituras gravadas em `leituras`, da mais nova para a mais velha. `?user_id=` filtra por
    pessoa e `?limite=` (ate LIMITE_GET_DADOS) limita. Inclui o que o payload trouxer, localizacao inclusive. Uma leitura
    so aparece depois de gravada (a fila de espera leva uma fracao de segundo)."""
    limite = request.args.get("limite", type=int) or const_borda.LIMITE_GET_DADOS
    limite = max(1, min(limite, const_borda.LIMITE_GET_DADOS))
    user_id = request.args.get("user_id", type=int)
    try:
        repo = repositorios.repo()
        linhas = [] if repo is None else repo.listar_leituras(user_id, limite)
    except Exception as e:  # noqa: BLE001
        print(f"  [dados] banco indisponivel: {type(e).__name__}")
        return jsonify({"status": "erro", "mensagem": "banco indisponivel"}), 503
    return jsonify([{"timestamp": l["instante_ingestao"].isoformat(), "user_id": l["user_id"], "source": l["fonte"],
                     "device_id": l["device_id"], "dados": l["payload"]} for l in linhas])


@app.route("/estado/<device_id>", methods=["GET"])
@exige("estado:ler")
def obter_estado_imobilidade(device_id):
    """Debug/demo: expoe o estado persistente de imobilidade de um dispositivo (`estado_dispositivo`, gravado a partir das
    decisoes da analise); procura em todos os usuarios."""
    try:
        repo = repositorios.repo()
        linha = None if repo is None else repo.ler_estado_por_dispositivo(device_id)
    except Exception as e:  # noqa: BLE001
        print(f"  [estado] banco indisponivel: {type(e).__name__}")
        return jsonify({"status": "erro", "mensagem": "banco indisponivel"}), 503
    if linha is None:
        return jsonify({"status": "erro", "mensagem": f"device_id desconhecido: {device_id}"}), 404
    return jsonify(linha["estado"])


@app.route("/health", methods=["GET"])
def health():
    """Disponibilidade da borda (`status`, como sempre) e a saude da analise: `analise.estado` e "degradado" quando a ultima
    chamada a ela falhou (o vigia a chama a cada INTERVALO_VIGIA_S, entao a queda aparece em instantes), `analise.fila` e o
    que espera por ela e `analise.ultima_resposta_ha_s` os segundos desde a ultima resposta boa (null se nunca respondeu).
    Sempre 200: a borda esta de pe mesmo com a analise parada."""
    with _fila_lock:
        espera = len(_fila_analise)
    degradado = _cliente is not None and _cliente.degradado
    ha_s = None if _cliente is None else _cliente.ultima_resposta_ha_s
    return jsonify({"status": "ok",
                    "analise": {"estado": "degradado" if degradado else "ok", "fila": espera,
                                "ultima_resposta_ha_s": ha_s},
                    "borda": _estatisticas_borda()})


@app.route("/", methods=["GET"])
def status():
    # Endpoint simples para o app/bridge checarem se o servidor esta de pe
    return jsonify({"status": "online"}), 200


# ---------------------------------------------------------------------------
# Inicializacao. Roda no import, para valer tambem em "flask --app borda.server run" e em servidores WSGI
# (o "flask" CLI usa o "app" do modulo): sem FLOW_JWT_SECRET valido a importacao falha, e as threads de descarga e de vigia
# sobem. FLOW_SEM_THREADS=1 desliga tudo isto (usado pela suite de testes, que liga a analise por `configurar_analise`).
# ---------------------------------------------------------------------------
_threads_lock = threading.Lock()
_threads_iniciadas = False


def iniciar_threads() -> None:
    """Sobe as threads de fundo uma unica vez: descarga das leituras + analise, o vigia dos episodios e a retencao."""
    global _threads_iniciadas, _thread_flush, _thread_vigia, _thread_retencao
    with _threads_lock:
        if _threads_iniciadas:
            return
        _parar_flush.clear()
        _thread_flush = threading.Thread(target=_loop, args=(ciclo_pipeline, const_borda.INTERVALO_DESCARGA_S),
                                         name="flow-flush", daemon=True)
        _thread_vigia = threading.Thread(target=_loop, args=(passo_vigia, const_borda.INTERVALO_VIGIA_S),
                                         name="flow-vigia", daemon=True)
        _thread_retencao = threading.Thread(target=_loop, args=(passo_retencao, const_borda.INTERVALO_RETENCAO_S, True),
                                            name="flow-retencao", daemon=True)
        _thread_flush.start()
        _thread_vigia.start()
        _thread_retencao.start()
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
    """DESLIGAMENTO ORDENADO da borda. Chamado por borda/servico.py depois de parar de aceitar conexoes:
    1. para as threads de fundo;
    2. encerra os streams SSE (os clientes reconectam quando a borda voltar);
    3. descarrega o que restou da fila de leituras;
    4. fecha o pool do banco.
    Idempotente. Leitura que nao coube no prazo ou no banco fora do ar e contada no log."""
    global _threads_iniciadas, _thread_flush, _thread_vigia, _thread_retencao
    prazo = const_borda.PRAZO_ENCERRAMENTO_S if prazo_s is None else prazo_s
    with _threads_lock:
        _parar_flush.set()
        for t in (_thread_flush, _thread_vigia, _thread_retencao):
            if t is not None:
                t.join(prazo)
        _thread_flush = _thread_vigia = _thread_retencao = None
        print(f"  [borda] encerrando: {HUB.fechar_todas()} stream(s) SSE fechado(s)")
        perdidas = _descarga_final(prazo)
        if perdidas:
            print(f"  [borda] AVISO: {perdidas} leitura(s) nao gravada(s) no encerramento (banco fora do ar ou prazo)")
        repositorios.desinstalar()
        _threads_iniciadas = False
        print("  [borda] encerrada")


if os.environ.get("FLOW_SEM_THREADS") != "1":
    auth.exigir_segredo()   # sem FLOW_JWT_SECRET o servidor nao sobe
    if os.environ.get("FLOW_DB_FAKE") == "1":
        repositorios.usar_fake()       # so desenvolvimento: banco em memoria, sem Docker
    else:
        repositorios.iniciar()         # sem banco (ou com esquema defasado) a borda nao sobe
    configurar_analise(cliente_analise.transporte_http())   # sem FLOW_ANALISE_TOKEN a borda nao sobe
    iniciar_threads()


if __name__ == "__main__":
    try:
        _contexto_tls = tls.contexto_servidor()
    except tls.ConfigTls as e:
        raise SystemExit(f"erro: {e}") from None
    print(f"Borda iniciando em https://{const_borda.HOST}:{const_borda.PORT} (modo de desenvolvimento; use 'python -m borda' para o desligamento ordenado)...")
    print(f"Configuracao (.env): {const_comum.DOTENV_ARQUIVO} " + ("carregado" if const_comum.DOTENV_CARREGADO
          else "nao encontrado; usando so as variaveis de ambiente"))
    print(f"Banco: {'banco falso em memoria (FLOW_DB_FAKE=1)' if os.environ.get('FLOW_DB_FAKE') == '1' else 'PostgreSQL'}"
          f" | analise: {const_comum.url_analise()}")
    print("Transporte: TLS (CA do projeto) + AES-256-GCM (chave compartilhada fixa, ver crypto_utils.py)")

    # FLOW_BORDA_HOST (padrao 127.0.0.1): para aceitar outros dispositivos da rede, o IP do computador no .env
    app.run(host=const_borda.HOST, port=const_borda.PORT, threaded=True,   # threaded: uma thread por stream SSE
            ssl_context=_contexto_tls)
