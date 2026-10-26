"""
Cliente da ANALISE (Servidor 2) do lado da borda: chama a API HTTP dela e cuida do que acontece quando ela cai.

    analisar(itens)   manda um lote (Leitura/Resposta) e devolve as `Decisoes`
    vigiar(agora)     pede o encerramento por expiracao dos episodios vencidos
    degradado         True quando a ultima chamada falhou (o /health mostra)
    ultima_resposta_ha_s  segundos desde a ultima resposta boa (None se nunca respondeu; o /health mostra)

Quando a analise some ou reinicia: cada resposta traz o `boot_id` dela. Se for diferente do ultimo conhecido (ou ainda
desconhecido), o estado em memoria dela foi perdido: antes de seguir, o cliente chama /restaurar com o que o banco sabe
(`montar_restauracao`) e REPETE a chamada que gerou a resposta duvidosa. Falha de rede, prazo estourado ou erro 5xx levantam
`AnaliseIndisponivel`: quem chama mantem o lote na fila e tenta de novo depois.

O transporte e uma funcao `(metodo, caminho, corpo, timeout_s) -> dict`: em producao `transporte_http` (HTTPS, confiando so
no certificado do projeto: comum/tls.py); nos testes, uma que chama `analise.api.Analise.tratar` direto, sem rede e sem TLS.
"""
import threading
import time
from typing import Callable

from borda import const as const_borda
from comum import const as const_comum
from comum import tls
from comum.contrato_analise import Decisoes


class AnaliseIndisponivel(RuntimeError):
    """Rede, prazo, erro 5xx ou token recusado: a analise nao respondeu. O lote deve ser tentado de novo."""


class RequisicaoRecusada(ValueError):
    """A analise recusou o corpo (400): reenviar nao adianta."""


def transporte_http(url: str | None = None, token: str | None = None, ca: str | None = None) -> Callable:
    """Transporte real: HTTPS para a analise, com o token no cabecalho. Le URL, token e CA na hora de criar.
    Falta de token, URL que nao seja https:// ou CA ausente levantam RuntimeError (a borda nao sobe)."""
    import requests
    token = token or const_comum.token_analise()
    url = tls.exigir_https(url or const_comum.url_analise(), "FLOW_ANALISE_URL").rstrip("/")
    ca = ca or tls.ca()                    # confianca: so a CA do projeto (nao as autoridades do sistema)
    sessao = requests.Session()

    def chamar(metodo: str, caminho: str, corpo: dict | None, timeout_s: float) -> dict:
        try:
            resp = sessao.request(metodo, url + caminho, json=corpo, timeout=timeout_s, verify=ca,
                                  headers={const_comum.ANALISE_CABECALHO_TOKEN: token})
        except requests.RequestException as e:
            raise AnaliseIndisponivel(f"{type(e).__name__}") from None
        if resp.status_code == 400:
            raise RequisicaoRecusada(caminho)
        if resp.status_code != 200:
            raise AnaliseIndisponivel(f"HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError:
            raise AnaliseIndisponivel("resposta nao e JSON") from None

    return chamar


class ClienteAnalise:
    def __init__(self, transporte: Callable, montar_restauracao: Callable[[], dict]):
        self._transporte = transporte
        self._montar_restauracao = montar_restauracao
        self._boot_id: str | None = None
        self._lock = threading.RLock()       # o laco de descarga e o vigia chamam de threads diferentes
        self.degradado = False
        self._ultima_resposta: float | None = None     # time.monotonic() da ultima resposta boa

    @property
    def ultima_resposta_ha_s(self) -> float | None:
        """Segundos desde a ultima resposta boa da analise (uma casa decimal); None se ela nunca respondeu."""
        ultima = self._ultima_resposta
        return None if ultima is None else round(max(0.0, time.monotonic() - ultima), 1)

    # ---- chamadas ----
    def _enviar(self, metodo: str, caminho: str, corpo: dict | None, timeout_s: float) -> dict:
        try:
            resposta = self._transporte(metodo, caminho, corpo, timeout_s)
        except AnaliseIndisponivel:
            self.degradado = True
            raise
        except RequisicaoRecusada:
            raise
        except Exception as e:  # noqa: BLE001  (qualquer falha de transporte conta como analise fora do ar)
            self.degradado = True
            raise AnaliseIndisponivel(type(e).__name__) from None
        self.degradado = False
        self._ultima_resposta = time.monotonic()
        return resposta

    def _restaurar(self) -> None:
        try:
            corpo = self._montar_restauracao()
        except Exception as e:  # noqa: BLE001  (banco fora do ar: nao da para restaurar agora)
            raise AnaliseIndisponivel(f"restauracao: {type(e).__name__}") from None
        resposta = self._enviar("POST", "/restaurar", corpo, const_borda.ANALISE_TIMEOUT_RESTAURAR_S)
        self._boot_id = resposta["boot_id"]

    def restaurar(self) -> None:
        """Manda agora o estado do banco a analise (a borda faz isso na primeira chamada e quando o boot_id muda)."""
        with self._lock:
            self._restaurar()

    def _chamar(self, caminho: str, corpo: dict) -> dict:
        """POST que so devolve a resposta de uma analise cujo estado foi restaurado."""
        with self._lock:
            for _ in range(2):
                if self._boot_id is None:
                    self._restaurar()
                resposta = self._enviar("POST", caminho, corpo, const_borda.ANALISE_TIMEOUT_S)
                if resposta.get("boot_id") == self._boot_id:
                    return resposta
                print("  [analise] boot_id novo: a analise reiniciou; restaurando o estado dela")
                self._boot_id = None
            raise AnaliseIndisponivel("a analise reiniciou de novo durante a restauracao")

    def analisar(self, itens: list) -> Decisoes:
        resposta = self._chamar("/analisar", {"itens": [i.para_json() for i in itens]})
        return Decisoes.de_json(resposta["decisoes"])

    def vigiar(self, agora) -> Decisoes:
        from comum.contrato_analise import instante_para_json
        resposta = self._chamar("/vigiar", {"agora": instante_para_json(agora)})
        return Decisoes.de_json(resposta["decisoes"])
