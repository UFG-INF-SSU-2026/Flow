"""
Analise de queda e ciclo de confirmacao (Marco 3).

Recebe leituras de aceleracao das duas fontes e decide se ha QUEDA PROVAVEL. Nao envia nada:
devolve um PedidoConfirmacao (server.py despacha pelos canais). Nao importa regra_imobilidade nem canais.

Gatilhos:
  A (preferencial) IMPACTO_SEGUIDO_DE_IMOBILIDADE  -> avaliar()              (app e wokwi)
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
    message_ids: set = field(default_factory=set)
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
class _FaseFonte:
    fase: str = "OBSERVANDO"        # OBSERVANDO | POS_IMPACTO
    t_impacto: float = 0.0


_fases: dict[tuple, _FaseFonte] = {}
_episodios: dict[int, Episodio] = {}      # episodio mais recente por usuario (aberto ou encerrado)
_msg_para_user: dict[str, int] = {}


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


# ------------------- deduplicacao da telemetria do app (DC-08) -------------------
_MAX_REFERENCIAS_DEDUP = 1000   # limite de dispositivos rastreados (device_id vem do payload do cliente)

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
        if len(_ultimo_ts_app) > _MAX_REFERENCIAS_DEDUP:
            _ultimo_ts_app.pop(next(iter(_ultimo_ts_app)))
        return False


# --------------------------- gatilho A (impacto) ---------------------------
def avaliar(user_id: int, fonte: str, dados: dict, agora: float | None = None) -> PedidoConfirmacao | None:
    agora = _agora() if agora is None else agora
    leitura = extrair_dinamica(fonte, dados)
    if leitura is None:
        return None
    din, din_max = leitura
    with _lock:
        f = _fases.setdefault((user_id, fonte), _FaseFonte())
        if f.fase == "OBSERVANDO":
            if din_max >= const.LIMIAR_IMPACTO[fonte]:
                f.fase, f.t_impacto = "POS_IMPACTO", agora
            return None
        # fase POS_IMPACTO
        if din >= const.LIMIAR_REPOUSO[fonte]:           # houve movimento: nao e queda
            f.fase = "OBSERVANDO"
            if din_max >= const.LIMIAR_IMPACTO[fonte]:   # ... mas pode ser novo impacto
                f.fase, f.t_impacto = "POS_IMPACTO", agora
            return None
        if agora - f.t_impacto >= const.T_POS_IMPACTO_S:
            f.fase = "OBSERVANDO"
            return _abrir(user_id, const.MOTIVO_A, agora)
        return None


# ------------------------ gatilho B (imobilidade) --------------------------
def abrir_por_imobilidade(user_id: int, agora: float | None = None) -> PedidoConfirmacao | None:
    return _abrir(user_id, const.MOTIVO_B, _agora() if agora is None else agora)


# ------------------------------- episodios ---------------------------------
def _abrir(user_id: int, motivo: str, agora: float) -> PedidoConfirmacao | None:
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
    registrar(user_id, "episodio.aberto", {"episodeId": novo.episode_id, "motivo": motivo})
    return PedidoConfirmacao(novo.episode_id, const.TIMEOUT_CONFIRMACAO_S, motivo)


def anotar_mensagem(user_id: int, episode_id: str, message_id: str) -> None:
    with _lock:
        ep = _episodios.get(user_id)
        if ep and ep.episode_id == episode_id:
            ep.message_ids.add(message_id)
        _msg_para_user[message_id] = user_id


def registrar_retorno(user_id: int, evento: dict, agora: float | None = None) -> Encerramento | None:
    """evento: dict normalizado por mensagens.validar_evento_retorno. Armazena e, se ESTOU_BEM num episodio
    aberto, encerra como CONFIRMADO."""
    agora = _agora() if agora is None else agora
    enc = None
    ep_id = None
    with _lock:
        ep = _episodios.get(user_id)
        if ep is not None:
            ep_id = ep.episode_id
        aberto = ep is not None and ep.encerrado_em is None and evento["messageId"] in ep.message_ids
        if evento["eventType"] == "confirmacao.resposta" and evento["state"] == "ESTOU_BEM":
            if aberto:
                ep.encerrado_em, ep.resultado = agora, "CONFIRMADO"
                enc = Encerramento(user_id, ep.episode_id, "CONFIRMADO")
            else:
                evento = {**evento, "tardia": True}
    registrar(user_id, evento["eventType"], {**evento, "episodeId": ep_id})
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
    for e in saida:
        registrar(e.user_id, "episodio.encerrado", {"episodeId": e.episode_id, "resultado": "EXPIRADO"})
    return saida


# ------------------------------- armazenamento -----------------------------
def registrar(user_id: int, tipo: str, dados: dict) -> None:
    """Uma linha em dataUsers/confirmacoes_usuario_<id>.jsonl. E tudo que o servidor faz com a resposta."""
    os.makedirs(const.DATA_DIR, exist_ok=True)
    linha = {"timestamp": datetime.now(timezone.utc).isoformat(), "tipo": tipo, **dados}
    with _lock, open(os.path.join(const.DATA_DIR, f"confirmacoes_usuario_{user_id}.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(linha, ensure_ascii=False) + "\n")
