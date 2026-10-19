"""
Banco falso em memoria: as duas camadas de acesso sem Docker (testes e modo FLOW_DB_FAKE=1).

Um unico BancoFalso entrega duas fachadas, cada uma com o seu papel. Cada operacao consulta a MESMA matriz de
permissoes que o banco real aplica (permissoes.py): escritor errado levanta PermissaoNegada, como o 'permission
denied' do Postgres levantaria. Tambem imita: ids sequenciais (IDENTITY), CHECKs principais, upsert de episodios e
estado, cursores de linha unica e transacao com desfazer (transacao()).
"""
import copy
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Sequence

from . import permissoes
from .repo_analise import RepoAnalise
from .repo_borda import RepoBorda

TABELAS = ("leituras", "respostas_dispositivo", "entregas_push", "comandos_push", "alertas", "confirmacoes",
           "episodios", "estado_dispositivo", "telemetria", "auditoria")
COM_ID = {"leituras", "respostas_dispositivo", "entregas_push", "comandos_push", "alertas", "confirmacoes", "auditoria"}

_FONTES = {"app", "wokwi"}
_CANAIS_COMANDO = {"app", "wokwi", "todos"}
_RESULTADOS = {"enviado", "sem_conexao", "expirado", "nao_suportado", "erro"}
_ESTADOS_EPISODIO = {"aberto", "aguardando_confirmacao", "encerrado"}


def _agora() -> datetime:
    return datetime.now(timezone.utc)


class BancoFalso:
    def __init__(self, relogio=_agora):
        self.relogio = relogio
        self._lock = threading.RLock()
        self._limpar()

    def _limpar(self) -> None:
        self.tabelas: dict[str, list[dict]] = {t: [] for t in TABELAS}
        self._ids: dict[str, int] = {t: 0 for t in COM_ID}
        agora = self.relogio()
        self.cursor_analise = {"ultimo_leitura_id": 0, "ultimo_leitura_instante": None, "ultimo_resposta_id": 0,
                               "ultimo_entrega_id": 0, "atualizado_em": agora, "versao_codigo": None, "metricas": {}}
        self.cursor_borda = {"ultimo_comando_id": 0, "ultimo_comando_instante": None, "atualizado_em": agora}

    def reiniciar(self) -> None:
        with self._lock:
            self._limpar()

    def borda(self) -> "FachadaBorda":
        return FachadaBorda(self)

    def analise(self) -> "FachadaAnalise":
        return FachadaAnalise(self)

    # ---- primitivas (todas conferem permissao) ----
    def _inserir(self, papel: str, tabela: str, linha: dict) -> dict:
        permissoes.exigir(papel, tabela, "I")
        with self._lock:
            nova = copy.deepcopy(linha)
            if tabela in COM_ID:
                self._ids[tabela] += 1
                nova = {"id": self._ids[tabela], **nova}
            self.tabelas[tabela].append(nova)
            return copy.deepcopy(nova)

    def _selecionar(self, papel: str, tabela: str, filtro=lambda l: True) -> list[dict]:
        permissoes.exigir(papel, tabela, "S")
        with self._lock:
            return [copy.deepcopy(l) for l in self.tabelas[tabela] if filtro(l)]

    def _ler_novas(self, papel, tabela, coluna_instante, depois_de_id, instante_minimo, limite) -> list[dict]:
        def filtro(l):
            return l["id"] > depois_de_id and (instante_minimo is None or l[coluna_instante] >= instante_minimo)
        return sorted(self._selecionar(papel, tabela, filtro), key=lambda l: l["id"])[:limite]

    def _cursor(self, papel: str, nome: str, op: str) -> dict:
        permissoes.exigir(papel, nome, op)
        return getattr(self, nome)

    # ---- transacao: guarda uma copia e restaura se algo levantar ----
    @contextmanager
    def transacao(self):
        with self._lock:
            copia = copy.deepcopy((self.tabelas, self._ids, self.cursor_analise, self.cursor_borda))
            try:
                yield
            except BaseException:
                self.tabelas, self._ids, self.cursor_analise, self.cursor_borda = copia
                raise


class FachadaBorda(RepoBorda):
    PAPEL = permissoes.BORDA

    def __init__(self, banco: BancoFalso):
        self._b = banco

    def gravar_leituras(self, leituras: Sequence[dict]) -> int:
        with self._b.transacao():     # o lote e tudo ou nada, como no banco real
            for l in leituras:
                if l["fonte"] not in _FONTES:
                    raise ValueError(f"fonte invalida: {l['fonte']!r}")
                self._b._inserir(self.PAPEL, "leituras", {
                    "instante_ingestao": l["instante_ingestao"], "user_id": l["user_id"], "device_id": l["device_id"],
                    "fonte": l["fonte"], "tipo_evento": l.get("tipo_evento"), "schema_version": l["schema_version"],
                    "instante_origem": l.get("instante_origem"), "conta_id": l.get("conta_id"), "payload": l["payload"]})
        return len(leituras)

    def gravar_telemetria(self, linhas: Sequence[dict]) -> int:
        for l in linhas:
            self._b._inserir(self.PAPEL, "telemetria", {"instante": l["instante"], "user_id": l["user_id"],
                                                        "app": l.get("app"), "wokwi": l.get("wokwi")})
        return len(linhas)

    def gravar_resposta(self, r: dict) -> None:
        if r["fonte"] not in _FONTES:
            raise ValueError(f"fonte invalida: {r['fonte']!r}")
        self._b._inserir(self.PAPEL, "respostas_dispositivo", {
            "instante_ingestao": r["instante_ingestao"], "user_id": r["user_id"], "device_id": r.get("device_id"),
            "fonte": r["fonte"], "tipo": r["tipo"], "message_id": r.get("message_id"),
            "schema_version": r["schema_version"], "payload": r["payload"]})

    def gravar_entrega(self, e: dict) -> None:
        if e["canal"] not in _FONTES or e["resultado"] not in _RESULTADOS:
            raise ValueError(f"canal/resultado invalido: {e['canal']!r}/{e['resultado']!r}")
        self._b._inserir(self.PAPEL, "entregas_push", {
            "instante": e["instante"], "comando_id": e.get("comando_id"), "message_id": e.get("message_id"),
            "user_id": e["user_id"], "device_id": e.get("device_id"), "canal": e["canal"],
            "resultado": e["resultado"], "detalhe": e.get("detalhe")})

    def gravar_auditoria(self, a: dict) -> None:
        self._b._inserir(self.PAPEL, "auditoria", {"instante": a["instante"], "rota": a.get("rota"),
                                                   "status": a["status"], "motivo": a["motivo"],
                                                   "conta": a.get("conta"), "ip": a.get("ip")})

    def ler_comandos_novos(self, depois_de_id, instante_minimo=None, limite=100):
        return self._b._ler_novas(self.PAPEL, "comandos_push", "criado_em", depois_de_id, instante_minimo, limite)

    def ler_cursor_borda(self) -> dict:
        return copy.deepcopy(self._b._cursor(self.PAPEL, "cursor_borda", "S"))

    def gravar_cursor_borda(self, ultimo_comando_id, ultimo_comando_instante) -> None:
        c = self._b._cursor(self.PAPEL, "cursor_borda", "U")
        c.update(ultimo_comando_id=ultimo_comando_id, ultimo_comando_instante=ultimo_comando_instante,
                 atualizado_em=self._b.relogio())

    def consultar_leituras(self, user_id, limite=100):
        linhas = self._b._selecionar(self.PAPEL, "leituras", lambda l: l["user_id"] == user_id)
        return sorted(linhas, key=lambda l: l["instante_ingestao"], reverse=True)[:limite]

    def listar_alertas(self, user_id, limite=50):
        linhas = self._b._selecionar(self.PAPEL, "alertas", lambda l: l["user_id"] == user_id)
        return sorted(linhas, key=lambda l: l["instante"], reverse=True)[:limite]

    def buscar_episodio(self, episodio_id):
        achados = self._b._selecionar(self.PAPEL, "episodios", lambda l: l["episodio_id"] == episodio_id)
        return achados[0] if achados else None

    def ler_estado_dispositivo(self, user_id, device_id):
        achados = self._b._selecionar(self.PAPEL, "estado_dispositivo",
                                      lambda l: l["user_id"] == user_id and l["device_id"] == device_id)
        return achados[0] if achados else None

    def ler_saude(self) -> dict:
        permissoes.exigir(self.PAPEL, "saude_pipeline", "S")
        with self._b._lock:
            agora = self._b.relogio()
            maior = max((l["id"] for l in self._b.tabelas["leituras"]), default=0)
            ca, cb = self._b.cursor_analise, self._b.cursor_borda
            ult = cb["ultimo_comando_instante"]
            return {"atraso_ids": maior - ca["ultimo_leitura_id"],
                    "idade_batimento_analise_s": (agora - ca["atualizado_em"]).total_seconds(),
                    "idade_ultimo_comando_entregue_s": (agora - ult).total_seconds() if ult else None}


class FachadaAnalise(RepoAnalise):
    PAPEL = permissoes.ANALISE

    def __init__(self, banco: BancoFalso):
        self._b = banco

    def ler_leituras_novas(self, depois_de_id, instante_minimo=None, limite=500):
        return self._b._ler_novas(self.PAPEL, "leituras", "instante_ingestao", depois_de_id, instante_minimo, limite)

    def ler_respostas_novas(self, depois_de_id, instante_minimo=None, limite=500):
        return self._b._ler_novas(self.PAPEL, "respostas_dispositivo", "instante_ingestao", depois_de_id,
                                  instante_minimo, limite)

    def ler_entregas_novas(self, depois_de_id, instante_minimo=None, limite=500):
        return self._b._ler_novas(self.PAPEL, "entregas_push", "instante", depois_de_id, instante_minimo, limite)

    def gravar_comando(self, c: dict) -> int:
        if c["canal"] not in _CANAIS_COMANDO:
            raise ValueError(f"canal invalido: {c['canal']!r}")
        return self._b._inserir(self.PAPEL, "comandos_push", {
            "criado_em": c["criado_em"], "user_id": c["user_id"], "device_id": c.get("device_id"),
            "canal": c["canal"], "message_id": c["message_id"], "tipo": c["tipo"], "payload": c["payload"],
            "expira_em": c.get("expira_em"), "episodio_id": c.get("episodio_id"),
            "leitura_id_origem": c.get("leitura_id_origem")})["id"]

    def gravar_alerta(self, a: dict) -> int:
        return self._b._inserir(self.PAPEL, "alertas", {
            "instante": a["instante"], "user_id": a["user_id"], "device_id": a.get("device_id"), "tipo": a["tipo"],
            "mensagem": a.get("mensagem"), "dados": a.get("dados", {}), "episodio_id": a.get("episodio_id"),
            "leitura_id_origem": a.get("leitura_id_origem"), "schema_version": a.get("schema_version")})["id"]

    def gravar_confirmacao(self, c: dict) -> int:
        return self._b._inserir(self.PAPEL, "confirmacoes", {
            "instante": c["instante"], "user_id": c["user_id"], "tipo": c["tipo"], "dados": c.get("dados", {}),
            "episodio_id": c.get("episodio_id"), "message_id": c.get("message_id"),
            "leitura_id_origem": c.get("leitura_id_origem"), "schema_version": c.get("schema_version")})["id"]

    def gravar_episodio(self, e: dict) -> None:
        if e["estado"] not in _ESTADOS_EPISODIO:
            raise ValueError(f"estado invalido: {e['estado']!r}")
        permissoes.exigir(self.PAPEL, "episodios", "I")
        with self._b._lock:
            existente = next((l for l in self._b.tabelas["episodios"] if l["episodio_id"] == e["episodio_id"]), None)
            if existente is None:
                self._b.tabelas["episodios"].append(copy.deepcopy({
                    "episodio_id": e["episodio_id"], "user_id": e["user_id"], "device_id": e.get("device_id"),
                    "tipo": e["tipo"], "estado": e["estado"], "aberto_em": e["aberto_em"],
                    "atualizado_em": e["atualizado_em"], "encerrado_em": e.get("encerrado_em"),
                    "motivo_encerramento": e.get("motivo_encerramento"), "dados": e.get("dados", {})}))
                return
            permissoes.exigir(self.PAPEL, "episodios", "U")     # o conflito vira UPDATE (como o ON CONFLICT)
            existente["estado"] = e["estado"]
            existente["atualizado_em"] = e["atualizado_em"]
            if e.get("encerrado_em") is not None:
                existente["encerrado_em"] = e["encerrado_em"]
            if e.get("motivo_encerramento") is not None:
                existente["motivo_encerramento"] = e["motivo_encerramento"]
            if existente["device_id"] is None:
                existente["device_id"] = e.get("device_id")
            existente["dados"] = {**existente["dados"], **copy.deepcopy(e.get("dados", {}))}

    def listar_episodios_abertos(self):
        achados = self._b._selecionar(self.PAPEL, "episodios", lambda l: l["estado"] != "encerrado")
        return sorted(achados, key=lambda l: l["aberto_em"])

    def gravar_estado_dispositivo(self, s: dict) -> None:
        permissoes.exigir(self.PAPEL, "estado_dispositivo", "I")
        with self._b._lock:
            existente = next((l for l in self._b.tabelas["estado_dispositivo"]
                              if l["user_id"] == s["user_id"] and l["device_id"] == s["device_id"]), None)
            novo = copy.deepcopy({"user_id": s["user_id"], "device_id": s["device_id"],
                                  "atualizado_em": s["atualizado_em"], "versao_regra": s["versao_regra"],
                                  "estado": s["estado"]})
            if existente is None:
                self._b.tabelas["estado_dispositivo"].append(novo)
            else:
                permissoes.exigir(self.PAPEL, "estado_dispositivo", "U")
                existente.update(novo)

    def ler_cursor(self) -> dict:
        return copy.deepcopy(self._b._cursor(self.PAPEL, "cursor_analise", "S"))

    def gravar_cursor(self, ultimo_leitura_id=None, ultimo_leitura_instante=None, ultimo_resposta_id=None,
                      ultimo_entrega_id=None, versao_codigo=None, metricas=None) -> None:
        c = self._b._cursor(self.PAPEL, "cursor_analise", "U")
        novos = {"ultimo_leitura_id": ultimo_leitura_id, "ultimo_leitura_instante": ultimo_leitura_instante,
                 "ultimo_resposta_id": ultimo_resposta_id, "ultimo_entrega_id": ultimo_entrega_id,
                 "versao_codigo": versao_codigo, "metricas": metricas}
        with self._b._lock:
            c.update({k: copy.deepcopy(v) for k, v in novos.items() if v is not None})
            c["atualizado_em"] = self._b.relogio()      # o batimento sempre renova

    @contextmanager
    def transacao(self):
        with self._b.transacao():
            yield self
