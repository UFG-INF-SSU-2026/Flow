"""
API da ANALISE (Servidor 2): uma API HTTP local que a borda chama. A analise so decide: nao tem banco, nao tem credencial de
dispositivo e nao entrega nada. Quem grava as decisoes e entrega o push e a borda.

Rotas (todas exigem o cabecalho X-Flow-Analise-Token; corpo e resposta em JSON):

  POST /analisar   {"itens": [Leitura|Resposta, ...]}   -> {"boot_id", "decisoes": Decisoes}
  POST /vigiar     {"agora": "<ISO 8601>"}              -> {"boot_id", "decisoes": Decisoes}   (expira episodios vencidos)
  POST /restaurar  {"leituras": [...], "episodios": [...], "mensagens": [...]}  -> {"boot_id", "ok": true}
  GET  /saude                                           -> {"boot_id", "ok": true}

O formato dos itens e das decisoes esta em comum/contrato_analise.py. `boot_id` e gerado a cada subida: se a borda ve outro
valor, sabe que a analise reiniciou (estado em memoria perdido) e chama /restaurar antes de continuar.

A classe `Analise` nao conhece HTTP: `tratar(metodo, caminho, corpo)` recebe e devolve dicts. Os testes chamam esse metodo
direto (sem rede); `criar_app` so poe o Flask na frente, com a checagem do token.
"""
import hmac
import threading
import uuid
from datetime import datetime, timezone
from typing import Callable

from flask import Flask, jsonify, request

from analise import const as const_analise
from analise import nucleo, regra_queda
from comum import const as const_comum
from comum import mensagens
from comum.contrato_analise import Decisoes, Leitura, de_json_item, instante_de_json


class RequisicaoInvalida(ValueError):
    """O corpo nao tem o formato do contrato. A mensagem nunca contem o conteudo recebido."""


class RotaDesconhecida(LookupError):
    pass


def _agora_real() -> datetime:
    return datetime.now(timezone.utc)


def _lista(corpo, chave: str) -> list:
    if not isinstance(corpo, dict) or not isinstance(corpo.get(chave), list):
        raise RequisicaoInvalida(f"campo '{chave}' ausente ou nao e uma lista")
    return corpo[chave]


class Analise:
    """O estado das regras e o que se faz com ele. Um lock serializa as chamadas (uma regra por vez)."""

    def __init__(self, relogio: Callable[[], datetime] = _agora_real, estado: nucleo.EstadoRegras | None = None):
        self.boot_id = uuid.uuid4().hex
        self.estado = estado if estado is not None else nucleo.novo_estado()
        self._relogio = relogio
        self._lock = threading.Lock()

    # ---- roteamento (sem HTTP) ----
    def tratar(self, metodo: str, caminho: str, corpo: dict | None = None) -> dict:
        if (metodo, caminho) == ("POST", "/analisar"):
            return self.analisar(corpo)
        if (metodo, caminho) == ("POST", "/vigiar"):
            return self.vigiar(corpo)
        if (metodo, caminho) == ("POST", "/restaurar"):
            return self.restaurar(corpo)
        if (metodo, caminho) == ("GET", "/saude"):
            return {"boot_id": self.boot_id, "ok": True}
        raise RotaDesconhecida(f"{metodo} {caminho}")

    # ---- /analisar ----
    def analisar(self, corpo: dict | None) -> dict:
        """Item malformado e contado e pulado (um item ruim nunca trava o lote); o corpo todo malformado e recusado."""
        itens = []
        for bruto in _lista(corpo, "itens"):
            try:
                itens.append(de_json_item(bruto, const_comum.FONTES_VALIDAS))
            except ValueError as e:
                print(f"  [analise] item malformado ignorado: {e}")
        with self._lock:
            decisoes = self._processar(itens)
        return {"boot_id": self.boot_id, "decisoes": decisoes.para_json()}

    def _processar(self, itens: list) -> Decisoes:
        """Em ordem de instante (leituras antes de respostas no empate). Item mais velho que IDADE_MAX_ACAO_S so reconstroi
        estado: a decisao e descartada e o episodio que ele abriu sai do estado na hora. Excecao: a resposta que encerra
        um episodio (o idoso respondeu enquanto a analise estava parada) e escrituracao de um episodio real e vale."""
        agora = self._relogio()
        total = Decisoes()
        for item in sorted(itens, key=lambda i: (i.instante, 0 if isinstance(i, Leitura) else 1)):
            eh_leitura = isinstance(item, Leitura)
            try:
                decisoes = (nucleo.processar_leitura if eh_leitura else nucleo.processar_resposta)(self.estado, item)
            except Exception as e:  # noqa: BLE001  (uma regra com bug nao pode travar o lote)
                print(f"  [analise] falha ao processar {'leitura' if eh_leitura else 'resposta'} user={item.user_id}: "
                      f"{type(e).__name__}")
                continue
            antiga = (agora - item.instante).total_seconds() > const_analise.IDADE_MAX_ACAO_S
            if antiga and not (not eh_leitura and decisoes.comandos):
                for c in decisoes.comandos:
                    regra_queda.descartar_episodio(c.user_id, c.episodio.episodio_id, self.estado.queda)
                continue
            total.somar(decisoes)
        return total

    # ---- /vigiar ----
    def vigiar(self, corpo: dict | None) -> dict:
        try:
            agora = instante_de_json((corpo or {}).get("agora"))
        except ValueError as e:
            raise RequisicaoInvalida(f"campo 'agora': {e}") from None
        with self._lock:
            decisoes = nucleo.vigiar(self.estado, agora)
        return {"boot_id": self.boot_id, "decisoes": decisoes.para_json()}

    # ---- /restaurar ----
    def restaurar(self, corpo: dict | None) -> dict:
        """Reconstroi o estado das regras depois de uma subida (da analise OU da borda). O estado e esvaziado e refeito NO
        LUGAR (quem guarda referencia as tabelas continua enxergando as mesmas).

        1. RELE `leituras` (so as dos ultimos segundos), em ordem; as decisoes sao descartadas: so interessa o que fica na
           memoria das regras (impacto, fusao, imobilidade, luz, deduplicacao).
        2. RESTAURA os episodios pelo que a borda leu do banco, nao pela releitura: reler uma queda ja tratada abriria
           um episodio novo. O aberto continua aberto com as mesmas `messageId`; o encerrado vale para o cooldown.
        O corpo e conferido por inteiro ANTES de mexer no estado: corpo ruim e recusado e o estado fica como estava."""
        leituras_json = _lista(corpo, "leituras")
        episodios = _lista(corpo, "episodios")
        mensagens_json = _lista(corpo, "mensagens")
        try:
            eps = []
            for e in sorted(episodios, key=lambda e: e["aberto_em"]):
                encerrado = e["estado"] == "encerrado"
                aberto_em = instante_de_json(e["aberto_em"])
                fim = (instante_de_json(e.get("encerrado_em"), obrigatorio=False)
                       or instante_de_json(e["atualizado_em"])) if encerrado else None
                eps.append((e["user_id"], e["episodio_id"], e["tipo"], aberto_em.timestamp(),
                            None if fim is None else fim.timestamp(), e.get("motivo_encerramento") if encerrado else None))
        except (KeyError, TypeError, ValueError):
            raise RequisicaoInvalida("episodio malformado na restauracao") from None
        itens = []
        for bruto in leituras_json:
            try:
                itens.append(de_json_item(bruto, const_comum.FONTES_VALIDAS))
            except ValueError as e:
                print(f"  [analise] leitura malformada na restauracao ignorada: {e}")
        por_episodio: dict[str, list[str]] = {}
        for m in mensagens_json:
            if isinstance(m, dict) and m.get("tipo") == mensagens.TIPO_SOLICITADA and m.get("message_id"):
                por_episodio.setdefault(m.get("episodio_id"), []).append(m["message_id"])
        with self._lock:
            nucleo.limpar_estado(self.estado)
            for leitura in sorted((i for i in itens if isinstance(i, Leitura)), key=lambda l: l.instante):
                try:
                    nucleo.processar_leitura(self.estado, leitura)      # decisoes descartadas: reconstrucao pura
                except Exception:  # noqa: BLE001  (uma regra com bug nao pode impedir a restauracao)
                    pass
            regra_queda.limpar_episodios(self.estado.queda)             # o que a releitura abriu e fantasma
            for user_id, episodio_id, tipo, aberto_em, fim, motivo in eps:
                regra_queda.restaurar_episodio(user_id, episodio_id, tipo, aberto_em, fim, motivo,
                                               por_episodio.get(episodio_id, ()), self.estado.queda)
        abertos = sum(1 for e in eps if e[4] is None)
        print(f"  [analise] restauracao: {len(itens)} leitura(s) relida(s), {len(eps)} episodio(s) restaurado(s), "
              f"{abertos} aberto(s)")
        return {"boot_id": self.boot_id, "ok": True}


def criar_app(analise: Analise, token: str) -> Flask:
    """O Flask na frente: confere o token (comparacao em tempo constante) e traduz erros em codigos HTTP."""
    app = Flask("flow-analise")
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
    app.json.default = str      # um tipo que o JSON nao conhece (as regras devolvem dicts livres) vira texto, como no banco

    @app.before_request
    def _conferir_token():
        enviado = request.headers.get(const_comum.ANALISE_CABECALHO_TOKEN, "")
        if not hmac.compare_digest(enviado.encode(), token.encode()):
            return jsonify({"erro": "token_invalido"}), 401

    @app.route("/<path:rota>", methods=["GET", "POST"])
    def _rota(rota):
        corpo = request.get_json(silent=True) if request.method == "POST" else None
        try:
            return jsonify(analise.tratar(request.method, "/" + rota, corpo))
        except RotaDesconhecida:
            return jsonify({"erro": "rota_desconhecida"}), 404
        except RequisicaoInvalida as e:
            return jsonify({"erro": "requisicao_invalida", "detalhe": str(e)}), 400

    return app
