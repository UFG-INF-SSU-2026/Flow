"""
Analise de queda e ciclo de confirmacao (Marco 3).

Recebe leituras de aceleracao das duas fontes e decide se ha QUEDA PROVAVEL. Nao envia nada:
devolve um PedidoConfirmacao (server.py despacha pelos canais). Nao importa regra_imobilidade nem canais.

Gatilhos:
  A (preferencial) IMPACTO_SEGUIDO_DE_IMOBILIDADE  -> avaliar()              (app e wokwi, fusao por usuario)
  B                IMOBILIDADE_PROLONGADA          -> abrir_por_imobilidade() (wokwi; chamado por server.py)

Um unico episodio aberto por user_id, com cooldown depois de encerrado.
"""
import json
import math
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import const   # todas as constantes (limiares, timeout, margem, cooldown...) vivem em const.py

_lock = threading.RLock()


@dataclass
class Episodio:
    episode_id: str
    motivo: str
    aberto_em: float
    prazo: float
    encerrado_em: float | None = None
    resultado: str | None = None


@dataclass
class PedidoConfirmacao:
    episode_id: str
    timeout_s: float
    motivo: str


@dataclass
class Encerramento:
    user_id: int
    episode_id: str
    resultado: str          # CONFIRMADO | EXPIRADO


@dataclass
class _Leitura:
    """Uma amostra aproveitavel de aceleracao (ja validada e deduplicada)."""
    fonte: str
    device_id: str
    versao: int
    din: float              # aceleracao dinamica atual (m/s2)
    din_max: float          # maximo da janela de 1 s (m/s2)
    t_chegada: float        # relogio do servidor (s)
    degradada: bool         # wokwi sem valueMax: din_max = din


@dataclass
class _EstadoUsuario:
    """Maquina do gatilho A: uma por usuario, alimentada por todas as fontes e dispositivos."""
    fase: str = "OBSERVANDO"        # OBSERVANDO | POS_IMPACTO
    t_impacto: float = 0.0
    t_ultima_leitura: float = 0.0   # ultima leitura USADA (desatualizada nao conta)
    leituras: dict = field(default_factory=dict)   # (fonte, device_id) -> ultima _Leitura do dispositivo


_estados: dict[int, _EstadoUsuario] = {}
_ultimo_log_desatualizada: dict[tuple, float] = {}   # (user_id, fonte, versao) -> instante do ultimo log
_episodios: dict[int, Episodio] = {}      # episodio mais recente por usuario (aberto ou encerrado)


@dataclass
class _Mensagem:
    user_id: int
    episode_id: str
    expira_em: float        # relogio do servidor (s); base da poda por const.RETENCAO_MENSAGENS_S


# messageId -> dono e episodio da mensagem. E a unica fonte para saber a que episodio um retorno pertence.
# Cresce a cada pedido emitido e e podado em vigiar().
_mensagens: dict[str, _Mensagem] = {}


def _agora() -> float:
    return datetime.now(timezone.utc).timestamp()


# ------------------------- entrada normalizada -----------------------------
def extrair_dinamica(fonte: str, dados: dict) -> tuple[float, float] | None:
    """Retorna (dinamica_atual, dinamica_maxima_da_janela) em m/s2, ou None se nao ha leitura aproveitavel."""
    try:
        if fonte == "wokwi":
            if dados.get("eventType") != "imobilidade.leitura":
                return None
            v = float(dados["value"])
            vmax = float(dados.get("valueMax", v))
            if not (const.ACEL_MIN_PLAUSIVEL <= v <= const.ACEL_MAX_PLAUSIVEL) \
                    or not (v <= vmax <= const.ACEL_MAX_PLAUSIVEL):
                return None
            return v, vmax
        if fonte == "app":
            a = dados.get("acelerometro")
            if not a:
                return None
            m, mmax = float(a["magnitude"]), float(a["magnitude_max_janela"])
            if not (const.ACEL_MIN_PLAUSIVEL <= m <= mmax <= const.ACEL_MAX_PLAUSIVEL):
                return None
            return abs(m - const.GRAVIDADE), max(mmax - const.GRAVIDADE, 0.0)
    except (KeyError, TypeError, ValueError):
        return None
    return None


# ------------------- deduplicacao da telemetria do app -------------------
_ultimo_ts_app: dict[tuple, float] = {}   # (user_id, "app", device_id) -> ultimo timestamp_envio (epoch ms)


def eh_duplicado_app(user_id: int, device_id: str, dados: dict) -> bool:
    """True se esta leitura do app e duplicata/atrasada: `timestamp_envio` (epoch ms, raiz do payload) menor ou
    igual ao ultimo aceito para (user_id, 'app', device_id). Se regredir mais de const.TOLERANCIA_RELOGIO_S, assume
    reajuste de relogio do celular: vira a nova referencia e NAO e duplicata (nao bloqueia a analise de queda).

    Nao e duplicata (retorna False, sem tocar no estado) a leitura sem `acelerometro` aproveitavel ou sem
    `timestamp_envio` numerico: nao ha o que deduplicar, e a funcao de seguranca nao deve ser silenciada."""
    if extrair_dinamica("app", dados) is None:
        return False
    ts = dados.get("timestamp_envio")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts):
        return False
    chave = (user_id, "app", device_id)
    with _lock:
        ultimo = _ultimo_ts_app.get(chave)
        if ultimo is not None and ts <= ultimo:
            if ultimo - ts <= const.TOLERANCIA_RELOGIO_S * 1000:
                return True                          # duplicata ou leitura atrasada
            # regressao grande: reajuste de relogio, cai na nova referencia abaixo
        _ultimo_ts_app.pop(chave, None)              # reinsere no fim: mantem a ordem de uso para o limite
        _ultimo_ts_app[chave] = ts
        if len(_ultimo_ts_app) > const.MAX_REFERENCIAS_DEDUP:
            _ultimo_ts_app.pop(next(iter(_ultimo_ts_app)))
        return False


# --------------------------- gatilho A (impacto) ---------------------------
def _versao_da_leitura(dados: dict) -> int:
    v = dados.get("schemaVersion", const.SCHEMA_VERSION_PADRAO)
    return v if isinstance(v, int) and not isinstance(v, bool) else const.SCHEMA_VERSION_PADRAO


def _log_desatualizada(user_id: int, leitura: _Leitura, referencia: int, agora: float) -> None:
    """Loga a leitura de versao menor que a de referencia, no maximo uma vez por const.LOG_DESATUALIZADA_INTERVALO_S
    para cada (usuario, fonte, versao). Chamar com _lock tomado."""
    chave = (user_id, leitura.fonte, leitura.versao)
    if agora - _ultimo_log_desatualizada.get(chave, float("-inf")) < const.LOG_DESATUALIZADA_INTERVALO_S:
        return
    _ultimo_log_desatualizada[chave] = agora
    print(f"  [queda] leitura desatualizada ignorada: user={user_id} fonte={leitura.fonte} "
          f"device={leitura.device_id} versao={leitura.versao} (referencia={referencia})")


def avaliar(user_id: int, fonte: str, dados: dict, agora: float | None = None,
            device_id: str | None = None) -> PedidoConfirmacao | None:
    """Gatilho A. Chamar uma vez por leitura aceita (validada e deduplicada), na chegada.

    Fusao de fontes por usuario:
      - versao de referencia = maior versao entre as leituras frescas (const.JANELA_FUSAO_S) de todos os
        dispositivos do usuario; so as leituras dessa versao sao USADAS. Leitura de versao menor e
        desatualizada: e logada e fica de fora (nao move a maquina nem renova o relogio dela);
      - entre leituras usadas vale o MAXIMO: cada leitura e comparada com o limiar da PROPRIA fonte, e qualquer
        uma pode abrir a suspeita (impacto) ou cancela-la (movimento). Nao ha media entre fontes;
      - cada leitura move a maquina uma unica vez, na chegada, e nao e recontada quando outra chega depois.

    Maquina: OBSERVANDO -(impacto)-> POS_IMPACTO -(sem movimento por T_POS_IMPACTO_S)-> episodio.
    Em POS_IMPACTO, "movimento" e din_max >= LIMIAR_REPOUSO (o pico da janela, nao a ultima amostra); se passar
    mais de const.INTERVALO_MAX_ENTRE_LEITURAS_S sem leitura usada, o impacto e descartado."""
    agora = _agora() if agora is None else agora
    leitura = extrair_dinamica(fonte, dados)
    if leitura is None:
        return None
    din, din_max = leitura
    nova = _Leitura(fonte, str(device_id if device_id is not None else f"user{user_id}"),
                    _versao_da_leitura(dados), din, din_max, agora,
                    degradada=(fonte == "wokwi" and "valueMax" not in dados))
    with _lock:
        u = _estados.setdefault(user_id, _EstadoUsuario())
        u.leituras[(nova.fonte, nova.device_id)] = nova
        # So ficam as leituras frescas; isso tambem limita a memoria (device_id vem do cliente).
        u.leituras = {k: l for k, l in u.leituras.items() if agora - l.t_chegada <= const.JANELA_FUSAO_S}
        referencia = max(l.versao for l in u.leituras.values())
        if nova.versao < referencia:
            _log_desatualizada(user_id, nova, referencia, agora)
            return None

        if u.fase == "POS_IMPACTO" and agora - u.t_ultima_leitura > const.INTERVALO_MAX_ENTRE_LEITURAS_S:
            u.fase = "OBSERVANDO"          # leituras pararam: o impacto nao vale mais
        u.t_ultima_leitura = agora

        impacto = din_max >= const.LIMIAR_IMPACTO[fonte]
        movimento = din_max >= const.LIMIAR_REPOUSO[fonte]
        if u.fase == "OBSERVANDO":
            if impacto:
                u.fase, u.t_impacto = "POS_IMPACTO", agora
            return None
        # fase POS_IMPACTO
        if movimento:                      # houve movimento: nao e queda (mas pode ser novo impacto)
            u.fase = "OBSERVANDO"
            if impacto:
                u.fase, u.t_impacto = "POS_IMPACTO", agora
            return None
        if agora - u.t_impacto >= const.T_POS_IMPACTO_S:
            u.fase = "OBSERVANDO"
            usadas = [l for l in u.leituras.values() if l.versao == referencia]
            antigas = [l for l in u.leituras.values() if l.versao < referencia]
            return _abrir(user_id, const.MOTIVO_A, agora, {
                "fonte_do_disparo": fonte,
                "versao_referencia": referencia,
                "fontes_usadas": [{"fonte": l.fonte, "device_id": l.device_id, "versao": l.versao,
                                   "degradada": l.degradada} for l in usadas],
                "fontes_desatualizadas": [{"fonte": l.fonte, "device_id": l.device_id, "versao": l.versao}
                                          for l in antigas],
            })
        return None


# ------------------------ gatilho B (imobilidade) --------------------------
def abrir_por_imobilidade(user_id: int, agora: float | None = None) -> PedidoConfirmacao | None:
    return _abrir(user_id, const.MOTIVO_B, _agora() if agora is None else agora)


# ------------------------------- episodios ---------------------------------
def _abrir(user_id: int, motivo: str, agora: float, extra: dict | None = None) -> PedidoConfirmacao | None:
    with _lock:
        ep = _episodios.get(user_id)
        if ep is not None:
            if ep.encerrado_em is None:
                return None                                   # ja ha episodio aberto
            if agora - ep.encerrado_em < const.COOLDOWN_S:
                return None                                   # cooldown
        novo = Episodio(str(uuid.uuid4()), motivo, agora,
                        agora + const.TIMEOUT_CONFIRMACAO_S + const.MARGEM_SERVIDOR_S)
        _episodios[user_id] = novo
    registrar(user_id, "episodio.aberto", {"episodeId": novo.episode_id, "motivo": motivo, **(extra or {})})
    return PedidoConfirmacao(novo.episode_id, const.TIMEOUT_CONFIRMACAO_S, motivo)


def anotar_mensagem(user_id: int, episode_id: str, message_id: str, agora: float | None = None) -> None:
    """Registra que `message_id` pertence a (user_id, episode_id). O episodio do retorno e sempre resolvido por aqui."""
    agora = _agora() if agora is None else agora
    with _lock:
        ep = _episodios.get(user_id)
        if ep is not None and ep.episode_id == episode_id:
            expira_em = ep.prazo
        else:
            expira_em = agora + const.TIMEOUT_CONFIRMACAO_S + const.MARGEM_SERVIDOR_S
        _mensagens[message_id] = _Mensagem(user_id, episode_id, expira_em)


def registrar_retorno(user_id: int, evento: dict, agora: float | None = None) -> Encerramento | None:
    """evento: dict normalizado por mensagens.validar_evento_retorno. Grava o retorno com o episodio da sua
    `messageId` e, se for ESTOU_BEM de mensagem conhecida cujo episodio ainda esta aberto, encerra como CONFIRMADO.

    - messageId desconhecida ou de outro usuario: `episodeId: null` e `mensagem_desconhecida: true`; nada encerra.
    - retorno de episodio ja encerrado (inclusive de um episodio anterior ao mais recente): `tardia: true`."""
    agora = _agora() if agora is None else agora
    enc = None
    extra: dict = {}
    with _lock:
        msg = _mensagens.get(evento["messageId"])
        if msg is None or msg.user_id != user_id:
            ep_id = None
            extra["mensagem_desconhecida"] = True
        else:
            ep_id = msg.episode_id
            ep = _episodios.get(user_id)
            # O episodio mais recente e o unico que pode estar aberto: um novo so abre depois que o anterior encerra.
            aberto = ep is not None and ep.episode_id == ep_id and ep.encerrado_em is None
            if aberto and evento["eventType"] == "confirmacao.resposta" and evento["state"] == "ESTOU_BEM":
                ep.encerrado_em, ep.resultado = agora, "CONFIRMADO"
                enc = Encerramento(user_id, ep_id, "CONFIRMADO")
            elif not aberto:
                extra["tardia"] = True
    registrar(user_id, evento["eventType"], {**evento, **extra, "episodeId": ep_id})
    if enc:
        registrar(user_id, "episodio.encerrado", {"episodeId": enc.episode_id, "resultado": "CONFIRMADO"})
    return enc


def vigiar(agora: float | None = None) -> list[Encerramento]:
    """Chamar a cada ~1 s: encerra como EXPIRADO os episodios com prazo vencido."""
    agora = _agora() if agora is None else agora
    saida = []
    with _lock:
        for uid, ep in _episodios.items():
            if ep.encerrado_em is None and agora >= ep.prazo:
                ep.encerrado_em, ep.resultado = agora, "EXPIRADO"
                saida.append(Encerramento(uid, ep.episode_id, "EXPIRADO"))
        _podar_mensagens(agora)
    for e in saida:
        # falha de gravacao (disco cheio, permissao) num episodio nao pode impedir que os demais
        # sejam registrados e devolvidos ao chamador (que despacha o encerramento). O episodio ja esta
        # marcado como encerrado acima; o erro so fica no log.
        try:
            registrar(e.user_id, "episodio.encerrado", {"episodeId": e.episode_id, "resultado": "EXPIRADO"})
        except Exception as exc:  # noqa: BLE001
            print(f"  [vigia] falha ao registrar encerramento episodeId={e.episode_id} user={e.user_id}: "
                  f"{type(exc).__name__}: {str(exc)[:200]}")
    return saida


def _podar_mensagens(agora: float) -> None:
    """Remove do mapa as mensagens que expiraram ha mais de const.RETENCAO_MENSAGENS_S. Chamar com _lock tomado.
    A retencao alem da validade existe para classificar como `tardia` as respostas que chegam depois do prazo."""
    corte = agora - const.RETENCAO_MENSAGENS_S
    for mid in [m for m, info in _mensagens.items() if info.expira_em < corte]:
        del _mensagens[mid]


# ------------------------------- armazenamento -----------------------------
def registrar(user_id: int, tipo: str, dados: dict) -> None:
    """Uma linha em dataUsers/confirmacoes_usuario_<id>.jsonl. E tudo que o servidor faz com a resposta."""
    os.makedirs(const.DATA_DIR, exist_ok=True)
    linha = {"timestamp": datetime.now(timezone.utc).isoformat(), "tipo": tipo, **dados}
    with _lock, open(os.path.join(const.DATA_DIR, f"confirmacoes_usuario_{user_id}.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(linha, ensure_ascii=False) + "\n")
