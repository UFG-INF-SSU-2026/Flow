"""
Roda TODOS os cenarios contra o sistema real (borda + analise), ponta a ponta.

Cenarios (cada um e uma lista de passos com tempo relativo; a mesma lista roda no teste sem rede, ver
tests/_transporte.py e tests/integration/test_acoes_banco_cenarios.py):
  descartes_wokwi           leitura repetida (duplicada) e fora de faixa: nenhuma acao, so descartes
  luz_inadequada            luz fria a noite: alerta apos leituras consecutivas, repetida e fora de faixa   [precisa do app]
  queda_estou_bem           queda -> pedido -> push.ack e "Estou bem" -> CONFIRMADO
  queda_sem_resposta        queda -> pedido -> so o ack -> EXPIRADO (vigia)
  queda_resposta_tardia     queda -> EXPIRADO -> "Estou bem" depois do prazo (registrada como tardia)
  imobilidade_gatilho_b     decisao ALERTA_IMOBILIDADE -> alerta + pedido -> "Estou bem"
  app_e_wokwi_juntos        o mesmo idoso com celular e Wokwi: fusao de fontes; o app confirma             [precisa do app]

Pre-requisitos: a borda E a analise no ar (`python -m borda` e `python -m analise`, ou scripts/tudo) e as contas do bridge
e, para os dois ultimos cenarios, do app (do MESMO usuario). Na pasta SensorServer:

    FLOW_BRIDGE_ID=bridge1 FLOW_BRIDGE_SECRET=<senha> [FLOW_APP_ID=idoso1 FLOW_APP_SECRET=<senha>] \\
        python simular_cenarios.py [URL_BASE] [--cenarios nome1,nome2] [--lista]

Entre dois cenarios que abrem episodio o script espera o cooldown do usuario (const_analise.COOLDOWN_S): o conjunto
inteiro leva alguns minutos. Ao final o script lista o que o banco deve ter recebido; confira as decisoes nas tabelas
`comandos_push`, `alertas`, `episodios` e `confirmacoes` (ver docs/banco.md). Nada aqui faz rede nem le argv/ambiente ao
importar (o teste importa este modulo).
"""
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field

from analise import const as const_analise
from borda.crypto_utils import decrypt_payload, encrypt_payload
from comum import const as const_comum

TIPO_PEDIDO = "queda.confirmacao_solicitada"


# ============================================================================
# Cenarios (dados puros)
# ============================================================================
@dataclass(frozen=True)
class Passo:
    t: float                        # segundos desde o inicio do cenario
    acao: str                       # nome de um metodo de Sessao
    args: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Cenario:
    nome: str
    descricao: str
    passos: tuple
    duracao_s: float                # o cenario so termina aqui: da tempo da analise alcancar e do vigia expirar
    precisa_app: bool = False
    abre_episodio: bool = False     # depois dele o script espera o cooldown


def _p(t, acao, **args) -> Passo:
    return Passo(t, acao, args)


def _queda(t0: float = 0.0) -> tuple[list, float]:
    """Leitura normal, impacto e T_POS_IMPACTO_S s de repouso. Devolve (passos, instante em que o pedido sai)."""
    passos = [_p(t0, "wokwi_leitura", v=1.0, vmax=1.5), _p(t0 + 1, "wokwi_leitura", v=3.0, vmax=25.0)]
    passos += [_p(t0 + 2 + k, "wokwi_leitura", v=0.3, vmax=0.4) for k in range(int(const_analise.T_POS_IMPACTO_S))]
    return passos, t0 + 1 + const_analise.T_POS_IMPACTO_S


def cenarios() -> list[Cenario]:
    """Os cenarios, com os prazos lidos das constantes na hora do uso."""
    expira = const_comum.TIMEOUT_CONFIRMACAO_S + const_comum.MARGEM_SERVIDOR_S      # do pedido ate o EXPIRADO
    # As respostas saem >= 1 s depois do pedido: o laco le `leituras` (descarga em lote) e `respostas_dispositivo` (gravada na
    # hora) em fluxos separados, e uma resposta nao pode ser lida antes da leitura que originou o pedido.
    folga = 3.0     # depois do ultimo efeito esperado: o laco da analise e o vigia precisam alcancar
    lista: list[Cenario] = []

    lista.append(Cenario(
        "descartes_wokwi", "Leitura repetida (mesmo sequence) e fora da faixa fisica: so descartes, nenhum episodio.",
        (_p(0, "wokwi_leitura", v=1.0, vmax=1.5), _p(1, "wokwi_repetir"), _p(2, "wokwi_leitura", v=200.0, vmax=200.0),
         _p(3, "wokwi_leitura", v=1.0, vmax=1.5)), 3 + folga))

    n = const_analise.LEITURAS_CONSECUTIVAS_PARA_ALERTA
    luz = [_p(k, "app_ambiente", cct=6500) for k in range(n + 1)]
    luz += [_p(n + 1, "app_ambiente", cct=6500, repetir=True), _p(n + 2, "app_ambiente", cct=20000)]
    lista.append(Cenario(
        "luz_inadequada", "Luz fria a noite: alerta na leitura consecutiva que completa o debounce; depois uma repetida "
        "(duplicada) e uma fora de faixa (descartada).", tuple(luz), n + 2 + folga, precisa_app=True))

    q, t_ped = _queda()
    lista.append(Cenario(
        "queda_estou_bem", "Queda pelo gatilho A; o bridge confirma o recebimento (push.ack) e responde \"Estou bem\".",
        tuple(q + [_p(t_ped + 1, "ack"), _p(t_ped + 2, "estou_bem")]), t_ped + 2 + folga, abre_episodio=True))

    q, t_ped = _queda()
    lista.append(Cenario(
        "queda_sem_resposta", "Queda pelo gatilho A; so o push.ack; ninguem responde: o vigia encerra como EXPIRADO.",
        tuple(q + [_p(t_ped + 1, "ack")]), t_ped + expira + folga, abre_episodio=True))

    q, t_ped = _queda()
    t_tarde = t_ped + expira + 2
    lista.append(Cenario(
        "queda_resposta_tardia", "Queda; o episodio expira; \"Estou bem\" chega depois do prazo (registro de resposta tardia).",
        tuple(q + [_p(t_ped + 1, "ack"), _p(t_tarde, "estou_bem")]), t_tarde + folga, abre_episodio=True))

    lista.append(Cenario(
        "imobilidade_gatilho_b", "Decisao ALERTA_IMOBILIDADE do firmware: alerta de imobilidade e pedido pelo gatilho B; "
        "depois \"Estou bem\".",
        (_p(0, "wokwi_leitura", v=1.0, vmax=1.5), _p(1, "wokwi_decisao", state="ALERTA_IMOBILIDADE"),
         _p(2, "ack"), _p(3, "estou_bem")), 3 + folga, abre_episodio=True))

    # O mesmo idoso com celular e Wokwi: o impacto vem do Wokwi, o repouso dos dois; o app responde "Estou bem".
    juntos = [_p(0, "wokwi_leitura", v=1.0, vmax=1.5), _p(0.5, "app_telemetria", magnitude=9.8, maximo=9.9),
              _p(1, "wokwi_leitura", v=3.0, vmax=25.0), _p(1.5, "app_telemetria", magnitude=9.8, maximo=9.9)]
    for k in range(int(const_analise.T_POS_IMPACTO_S)):
        juntos += [_p(2 + k, "wokwi_leitura", v=0.3, vmax=0.4), _p(2.5 + k, "app_telemetria", magnitude=9.8, maximo=9.9)]
    t_ped = 1 + const_analise.T_POS_IMPACTO_S
    juntos += [_p(t_ped + 1, "ack"), _p(t_ped + 1.5, "ack", canal="app"), _p(t_ped + 2.5, "estou_bem", canal="app")]
    lista.append(Cenario(
        "app_e_wokwi_juntos", "Celular e Wokwi do mesmo usuario na mesma janela (fusao de fontes); os dois confirmam o "
        "recebimento e o app responde \"Estou bem\".", tuple(juntos), t_ped + 2.5 + folga, precisa_app=True,
        abre_episodio=True))
    return lista


# ============================================================================
# Sessao: monta os payloads e os envia por um transporte
# ============================================================================
class Sessao:
    """Monta os payloads de um cenario e os entrega a `transporte`. O transporte (HTTP real aqui, Flask no teste) so
    precisa de:  postar(rota, fonte, corpo) -> status HTTP;  esperar_ate(t_relativo);  ultimo_message_id() -> str | None
    (o `messageId` do ultimo pedido de confirmacao entregue).

    Os contadores (sequence do wokwi, timestamp_envio e seq_num do app) so crescem durante a vida da Sessao: reuse a
    mesma Sessao para varios cenarios contra o MESMO servidor, senao as leituras viram duplicadas."""

    def __init__(self, transporte, user_id: int, device_wokwi: str, device_app: str | None,
                 seq_inicial: int = 1, ts_app_inicial_ms: int = 1_760_000_000_000):
        self.t = transporte
        self.user_id = user_id
        self.device_wokwi = device_wokwi
        self.device_app = device_app
        self._seq = seq_inicial
        self._ts_app = ts_app_inicial_ms
        self._seq_amb = seq_inicial
        self._ultimo_wokwi: dict | None = None
        self._ultimo_amb: dict | None = None
        self.status: list[tuple] = []       # (acao, rota, status HTTP) de tudo que foi enviado

    # ---- contadores ----
    def _proxima_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _enviar(self, acao: str, rota: str, fonte: str, corpo: dict) -> int:
        status = self.t.postar(rota, fonte, corpo)
        self.status.append((acao, rota, status))
        return status

    # ---- wokwi ----
    def _evento_wokwi(self, tipo: str, state: str, value: float, vmax: float | None = None) -> dict:
        seq = self._proxima_seq()
        ev = {"schemaVersion": const_comum.SCHEMA_VERSION_WOKWI, "eventType": tipo, "deviceId": self.device_wokwi,
              "entityId": "idoso-sim", "eventTimeMs": seq * 1000, "sequence": seq, "value": value, "unit": "m/s2",
              "state": state, "user_id": self.user_id, "source": "wokwi"}
        if vmax is not None:
            ev["valueMax"] = vmax
        return ev

    def wokwi_leitura(self, v: float, vmax: float | None = None) -> None:
        self._ultimo_wokwi = self._evento_wokwi("imobilidade.leitura", "NORMAL", v, vmax)
        self._enviar("wokwi_leitura", "/dados", "wokwi", self._ultimo_wokwi)

    def wokwi_decisao(self, state: str, value: float = 0.3) -> None:
        self._ultimo_wokwi = self._evento_wokwi("imobilidade.decisao", state, value)
        self._enviar("wokwi_decisao", "/dados", "wokwi", self._ultimo_wokwi)

    def wokwi_repetir(self) -> None:
        """Reenvia o ultimo evento do wokwi sem mudar o `sequence`: a analise deve descarta-lo como duplicado."""
        if self._ultimo_wokwi is not None:
            self._enviar("wokwi_repetir", "/dados", "wokwi", self._ultimo_wokwi)

    # ---- app ----
    def _base_app(self) -> dict:
        self._ts_app += 1000
        return {"schemaVersion": const_comum.SCHEMA_VERSION_APP, "source": "app", "user_id": self.user_id,
                "device_id": self.device_app, "timestamp_envio": self._ts_app}

    def app_telemetria(self, magnitude: float, maximo: float) -> None:
        corpo = {**self._base_app(), "acelerometro": {"magnitude": magnitude, "magnitude_max_janela": maximo}}
        self._enviar("app_telemetria", "/dados", "app", corpo)

    def app_ambiente(self, cct: float, luminosidade: float = 40.0, repetir: bool = False) -> None:
        """Leitura de ambiente as 22h (a regra de luz so vale a noite). `repetir` reenvia o ultimo bloco (mesmo seq_num)."""
        if repetir and self._ultimo_amb is not None:
            bloco = self._ultimo_amb
        else:
            self._seq_amb += 1
            bloco = {"schemaVersion": const_comum.SCHEMA_VERSION_APP, "device_id": self.device_app,
                     "event_time": "2026-10-14T22:00:00+00:00", "luminosidade": luminosidade, "cct": cct,
                     "seq_num": self._seq_amb}
            self._ultimo_amb = bloco
        self._enviar("app_ambiente", "/dados", "app", {**self._base_app(), "leitura_ambiente": bloco})

    # ---- respostas aos pedidos de confirmacao ----
    def _retorno(self, acao: str, canal: str, tipo: str, state: str) -> None:
        mid = self.t.ultimo_message_id()
        if mid is None:
            print(f"  [cenario] {acao}: nenhum pedido de confirmacao recebido (nada a responder)")
            return
        if canal == "app":
            corpo = {"schemaVersion": const_comum.SCHEMA_VERSION_APP, "source": "app", "user_id": self.user_id,
                     "device_id": self.device_app, "event_time": "2026-10-14T12:00:00.000Z", "eventType": tipo,
                     "state": state, "messageId": mid}
        else:
            seq = self._proxima_seq()
            corpo = {"schemaVersion": const_comum.SCHEMA_VERSION_WOKWI, "source": "wokwi", "user_id": self.user_id,
                     "deviceId": self.device_wokwi, "entityId": "idoso-sim", "eventTimeMs": seq * 1000, "sequence": seq,
                     "eventType": tipo, "state": state, "messageId": mid}
        self._enviar(acao, "/eventos", canal, corpo)

    def ack(self, canal: str = "wokwi", state: str = "ATIVADO") -> None:
        self._retorno("ack", canal, "push.ack", state)

    def estou_bem(self, canal: str = "wokwi", state: str = "ESTOU_BEM") -> None:
        self._retorno("estou_bem", canal, "confirmacao.resposta", state)


def executar(cenario: Cenario, sessao: Sessao) -> None:
    """Roda os passos do cenario no tempo relativo e espera ate `duracao_s`."""
    for passo in sorted(cenario.passos, key=lambda p: p.t):
        sessao.t.esperar_ate(passo.t)
        getattr(sessao, passo.acao)(**passo.args)
    sessao.t.esperar_ate(cenario.duracao_s)


# ============================================================================
# Transporte HTTP real
# ============================================================================
def _env(obj: dict) -> dict:
    n, c = encrypt_payload(json.dumps(obj))
    return {"nonce": n, "ciphertext": c}


class TransporteHttp:
    """Fala com o servidor como o bridge e o app: login, POST cifrado e SSE do bridge (para ler o `messageId` do pedido)."""

    def __init__(self, base: str, bridge: tuple[str, str, str], app: tuple[str, str, str] | None = None):
        """`bridge` e `app`: (conta, senha, device_id)."""
        import requests
        self._r = requests
        self.base = base.rstrip("/")
        self._cab: dict[str, dict] = {}
        self.user_id, self.user_app = None, None
        tok = self._login(*bridge, "wokwi")
        self._cab["wokwi"] = {"Authorization": f"Bearer {tok['access_token']}"}
        self.user_id = tok["user_ids"][0]
        if app is not None:
            tok_app = self._login(*app, "app")
            self._cab["app"] = {"Authorization": f"Bearer {tok_app['access_token']}"}
            self.user_app = tok_app["user_ids"][0]
        self._t0 = time.monotonic()
        self._pedidos: list[tuple[float, str]] = []     # (instante monotonico, messageId)
        self._lock = threading.Lock()
        threading.Thread(target=self._ouvir_sse, daemon=True).start()
        time.sleep(1)

    @property
    def tem_app(self) -> bool:
        return "app" in self._cab and self.user_app == self.user_id

    def _login(self, conta: str, senha: str, device_id: str, fonte: str) -> dict:
        r = self._r.post(f"{self.base}/auth/login", json=_env({
            "schemaVersion": const_comum.SCHEMA_VERSION_AUTH, "identificador": conta, "segredo": senha, "fonte": fonte,
            "device_id": device_id}), timeout=10)
        r.raise_for_status()
        j = r.json()
        return json.loads(decrypt_payload(j["nonce"], j["ciphertext"]))

    def _ouvir_sse(self) -> None:
        try:
            with self._r.get(f"{self.base}/push/wokwi", headers=self._cab["wokwi"], stream=True, timeout=(5, None)) as r:
                r.raise_for_status()
                evento = None
                for linha in r.iter_lines(decode_unicode=True):
                    if linha.startswith("event:"):
                        evento = linha[6:].strip()
                    elif linha.startswith("data:") and evento == "push":
                        env = json.loads(linha[5:].strip())
                        msg = json.loads(decrypt_payload(env["nonce"], env["ciphertext"]))
                        if msg.get("messageType") == TIPO_PEDIDO:
                            with self._lock:
                                self._pedidos.append((time.monotonic(), msg["messageId"]))
        except Exception as e:  # noqa: BLE001
            print(f"  [sse] stream do bridge encerrado: {type(e).__name__}: {str(e)[:150]}")

    # ---- interface de Sessao ----
    def iniciar_cenario(self) -> None:
        self._t0 = time.monotonic()

    def esperar_ate(self, t_rel: float) -> None:
        restante = self._t0 + t_rel - time.monotonic()
        if restante > 0:
            time.sleep(restante)

    def postar(self, rota: str, fonte: str, corpo: dict) -> int:
        r = self._r.post(self.base + rota, json=_env(corpo), headers=self._cab[fonte], timeout=10)
        if r.status_code != 200:
            print(f"  [cenario] {rota} ({fonte}) -> HTTP {r.status_code}")
        return r.status_code

    def ultimo_message_id(self) -> str | None:
        """messageId do ultimo pedido recebido DEPOIS do inicio do cenario (espera ate 5 s pelo SSE)."""
        fim = time.monotonic() + 5.0
        while True:
            with self._lock:
                recentes = [mid for t, mid in self._pedidos if t >= self._t0]
            if recentes or time.monotonic() >= fim:
                return recentes[-1] if recentes else None
            time.sleep(0.05)


# ============================================================================
# Linha de comando
# ============================================================================
def _argumentos(argv: list[str]) -> tuple[str, list[str] | None, bool]:
    base, escolhidos, so_lista = "http://localhost:5000", None, False
    it = iter(argv)
    for a in it:
        if a == "--lista":
            so_lista = True
        elif a == "--cenarios":
            escolhidos = [n.strip() for n in next(it, "").split(",") if n.strip()]
        elif a.startswith("http"):
            base = a
    return base, escolhidos, so_lista


def main(argv: list[str] | None = None) -> int:
    base, escolhidos, so_lista = _argumentos(sys.argv[1:] if argv is None else argv)
    todos = cenarios()
    if so_lista:
        for c in todos:
            print(f"{c.nome:26s} {'[app] ' if c.precisa_app else '      '}{c.descricao}")
        return 0
    if escolhidos:
        desconhecidos = [n for n in escolhidos if n not in {c.nome for c in todos}]
        if desconhecidos:
            print(f"cenario(s) desconhecido(s): {', '.join(desconhecidos)} (veja --lista)")
            return 2
        todos = [c for c in todos if c.nome in escolhidos]

    bridge = (os.environ.get("FLOW_BRIDGE_ID", "bridge1"), os.environ["FLOW_BRIDGE_SECRET"],
              os.environ.get("FLOW_BRIDGE_DEVICE", "simulador-cenarios"))
    app = None
    if os.environ.get("FLOW_APP_ID") and os.environ.get("FLOW_APP_SECRET"):
        app = (os.environ["FLOW_APP_ID"], os.environ["FLOW_APP_SECRET"], os.environ.get("FLOW_APP_DEVICE", "simulador-cenarios-app"))
    transporte = TransporteHttp(base, bridge, app)
    if app is not None and not transporte.tem_app:
        print(f"AVISO: a conta do app e do user_id {transporte.user_app} e a do bridge, do {transporte.user_id}: "
              "precisam ser do MESMO usuario. Cenarios do app serao pulados.")
    sessao = Sessao(transporte, transporte.user_id, bridge[2], app[2] if app else None,
                    seq_inicial=int(time.time()), ts_app_inicial_ms=int(time.time() * 1000))

    concluidos: list[str] = []
    print(f"Rodando {len(todos)} cenario(s) em {base} (user_id={transporte.user_id})")
    for i, c in enumerate(todos):
        if c.precisa_app and not transporte.tem_app:
            print(f"\n[{c.nome}] pulado: precisa de FLOW_APP_ID e FLOW_APP_SECRET (conta do app do mesmo usuario)")
            continue
        print(f"\n[{c.nome}] {c.descricao}")
        transporte.iniciar_cenario()
        executar(c, sessao)
        concluidos.append(c.nome)
        if c.abre_episodio and i + 1 < len(todos):
            espera = const_analise.COOLDOWN_S + 2
            print(f"  aguardando {espera} s (cooldown do usuario)...")
            time.sleep(espera)
    erros = [s for s in sessao.status if s[2] != 200]
    print(f"\nFim. {len(concluidos)} cenario(s), {len(sessao.status)} envio(s), {len(erros)} com HTTP diferente de 200.")
    print("Confira as decisoes no banco: comandos_push, alertas, episodios e confirmacoes (usuario %s)." % transporte.user_id)
    return 1 if erros else 0


if __name__ == "__main__":
    sys.exit(main())
