"""
Banco falso em memoria: a interface `Repo` sem Docker (testes e `FLOW_DB_FAKE=1`).

Imita: ids sequenciais (IDENTITY), CHECKs principais, message_id unico em comandos_push, id unico em contas, uso unico das
sessoes, upsert de episodios e estado e transacao com desfazer (`transacao()`). As tabelas ficam em `tabelas[nome]` (listas de dicts) para os testes inspecionarem.
"""
import copy
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Sequence

from .repo import Repo

TABELAS = ("leituras", "comandos_push", "alertas", "confirmacoes", "episodios", "estado_dispositivo", "contas", "sessoes")
COM_ID = {"leituras", "comandos_push", "alertas", "confirmacoes"}

_FONTES = {"app", "wokwi"}
_CANAIS_COMANDO = {"app", "wokwi", "todos"}
_ESTADOS_EPISODIO = {"aberto", "aguardando_confirmacao", "encerrado"}


def _agora() -> datetime:
    return datetime.now(timezone.utc)


class BancoFalso(Repo):
    def __init__(self, relogio=_agora):
        self.relogio = relogio
        self._lock = threading.RLock()
        self._limpar()

    def _limpar(self) -> None:
        self.tabelas: dict[str, list[dict]] = {t: [] for t in TABELAS}
        self._ids: dict[str, int] = {t: 0 for t in COM_ID}

    def reiniciar(self) -> None:
        with self._lock:
            self._limpar()

    def _inserir(self, tabela: str, linha: dict) -> dict:
        with self._lock:
            nova = copy.deepcopy(linha)
            if tabela in COM_ID:
                self._ids[tabela] += 1
                nova = {"id": self._ids[tabela], **nova}
            self.tabelas[tabela].append(nova)
            return copy.deepcopy(nova)

    def _selecionar(self, tabela: str, filtro=lambda l: True) -> list[dict]:
        with self._lock:
            return [copy.deepcopy(l) for l in self.tabelas[tabela] if filtro(l)]

    @contextmanager
    def transacao(self):
        """Guarda uma copia e restaura se algo levantar."""
        with self._lock:
            copia = copy.deepcopy((self.tabelas, self._ids))
            try:
                yield self
            except BaseException:
                self.tabelas, self._ids = copia
                raise

    # ---- leituras ----
    def gravar_leituras(self, leituras: Sequence[dict]) -> int:
        with self.transacao():     # o lote e tudo ou nada, como no banco real
            for l in leituras:
                if l["fonte"] not in _FONTES:
                    raise ValueError(f"fonte invalida: {l['fonte']!r}")
                self._inserir("leituras", {
                    "instante_ingestao": l["instante_ingestao"], "user_id": l["user_id"], "device_id": l["device_id"],
                    "fonte": l["fonte"], "tipo_evento": l.get("tipo_evento"), "schema_version": l["schema_version"],
                    "instante_origem": l.get("instante_origem"), "conta_id": l.get("conta_id"), "payload": l["payload"]})
        return len(leituras)

    def ler_leituras_recentes(self, desde, antes_de=None, limite=5000):
        def filtro(l):
            return l["instante_ingestao"] >= desde and (antes_de is None or l["instante_ingestao"] < antes_de)
        linhas = sorted(self._selecionar("leituras", filtro), key=lambda l: l["id"])
        return linhas[-limite:] if limite > 0 else []

    # ---- decisoes ----
    def gravar_comando(self, c: dict) -> int | None:
        if c["canal"] not in _CANAIS_COMANDO:
            raise ValueError(f"canal invalido: {c['canal']!r}")
        with self._lock:
            if any(l["message_id"] == c["message_id"] for l in self.tabelas["comandos_push"]):
                return None
            return self._inserir("comandos_push", {
                "criado_em": c["criado_em"], "user_id": c["user_id"], "device_id": c.get("device_id"),
                "canal": c["canal"], "message_id": c["message_id"], "tipo": c["tipo"], "payload": c["payload"],
                "expira_em": c.get("expira_em"), "episodio_id": c.get("episodio_id")})["id"]

    def gravar_alerta(self, a: dict) -> int:
        return self._inserir("alertas", {
            "instante": a["instante"], "user_id": a["user_id"], "device_id": a.get("device_id"), "tipo": a["tipo"],
            "mensagem": a.get("mensagem"), "dados": a.get("dados", {}), "episodio_id": a.get("episodio_id"),
            "schema_version": a.get("schema_version")})["id"]

    def gravar_confirmacao(self, c: dict) -> int:
        return self._inserir("confirmacoes", {
            "instante": c["instante"], "user_id": c["user_id"], "tipo": c["tipo"], "dados": c.get("dados", {}),
            "episodio_id": c.get("episodio_id"), "message_id": c.get("message_id"),
            "schema_version": c.get("schema_version")})["id"]

    def gravar_episodio(self, e: dict) -> None:
        if e["estado"] not in _ESTADOS_EPISODIO:
            raise ValueError(f"estado invalido: {e['estado']!r}")
        with self._lock:
            existente = next((l for l in self.tabelas["episodios"] if l["episodio_id"] == e["episodio_id"]), None)
            if existente is None:
                self.tabelas["episodios"].append(copy.deepcopy({
                    "episodio_id": e["episodio_id"], "user_id": e["user_id"], "device_id": e.get("device_id"),
                    "tipo": e["tipo"], "estado": e["estado"], "aberto_em": e["aberto_em"],
                    "atualizado_em": e["atualizado_em"], "encerrado_em": e.get("encerrado_em"),
                    "motivo_encerramento": e.get("motivo_encerramento"), "dados": e.get("dados", {})}))
                return
            existente["estado"] = e["estado"]
            existente["atualizado_em"] = e["atualizado_em"]
            if e.get("encerrado_em") is not None:
                existente["encerrado_em"] = e["encerrado_em"]
            if e.get("motivo_encerramento") is not None:
                existente["motivo_encerramento"] = e["motivo_encerramento"]
            if existente["device_id"] is None:
                existente["device_id"] = e.get("device_id")
            existente["dados"] = {**existente["dados"], **copy.deepcopy(e.get("dados", {}))}

    def gravar_estado_dispositivo(self, s: dict) -> None:
        with self._lock:
            existente = next((l for l in self.tabelas["estado_dispositivo"]
                              if l["user_id"] == s["user_id"] and l["device_id"] == s["device_id"]), None)
            novo = copy.deepcopy({"user_id": s["user_id"], "device_id": s["device_id"],
                                  "atualizado_em": s["atualizado_em"], "versao_regra": s["versao_regra"],
                                  "estado": s["estado"]})
            if existente is None:
                self.tabelas["estado_dispositivo"].append(novo)
            else:
                existente.update(novo)

    # ---- consultas ----
    def ler_estado_por_dispositivo(self, device_id):
        achados = self._selecionar("estado_dispositivo", lambda l: l["device_id"] == device_id)
        return max(achados, key=lambda l: l["atualizado_em"]) if achados else None

    def listar_episodios_recentes(self, desde):
        achados = self._selecionar("episodios", lambda l: l["estado"] != "encerrado" or l["atualizado_em"] >= desde)
        return sorted(achados, key=lambda l: l["aberto_em"])

    def listar_comandos_de_episodios(self, episodio_ids):
        ids = set(episodio_ids)
        achados = self._selecionar("comandos_push", lambda l: l.get("episodio_id") in ids)
        return sorted(({k: l.get(k) for k in ("id", "episodio_id", "message_id", "tipo", "criado_em", "expira_em")}
                       for l in achados), key=lambda l: l["id"])

    # ---- contas e sessoes de login ----
    def criar_conta(self, c: dict) -> bool:
        with self._lock:
            if any(l["id"] == c["id"] for l in self.tabelas["contas"]):
                return False
            self._inserir("contas", {"id": c["id"], "papel": c["papel"], "user_ids": list(c["user_ids"]),
                                     "fontes": list(c["fontes"]), "hash": c["hash"], "criada_em": c["criada_em"]})
            return True

    def buscar_conta(self, conta_id):
        achadas = self._selecionar("contas", lambda l: l["id"] == conta_id)
        return achadas[0] if achadas else None

    def gravar_sessao(self, s: dict) -> None:
        with self._lock:
            if any(l["token_hash"] == s["token_hash"] for l in self.tabelas["sessoes"]):
                raise ValueError("token_hash ja existe")
            self._inserir("sessoes", {"token_hash": s["token_hash"], "conta_id": s["conta_id"], "fonte": s.get("fonte"),
                                      "device_id": s.get("device_id"), "expira_em": s["expira_em"]})

    def consumir_sessao(self, token_hash):
        with self._lock:
            achada = next((l for l in self.tabelas["sessoes"] if l["token_hash"] == token_hash), None)
            if achada is None:
                return None
            self.tabelas["sessoes"].remove(achada)
            return copy.deepcopy(achada)

    def podar_sessoes(self, agora) -> int:
        with self._lock:
            antes = len(self.tabelas["sessoes"])
            self.tabelas["sessoes"] = [l for l in self.tabelas["sessoes"] if l["expira_em"] > agora]
            return antes - len(self.tabelas["sessoes"])
