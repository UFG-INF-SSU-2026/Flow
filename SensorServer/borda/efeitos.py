"""
Aplicar as decisoes da analise: gravar no banco e, so depois do commit, entregar o push.

`Aplicador.aplicar(decisoes)` grava tudo NUMA transacao (registros de confirmacao, alertas, estado e, por fim, cada comando:
episodio + comando juntos). Se o banco falhar, nada foi gravado, o push NAO sai e as decisoes ficam guardadas para a proxima
tentativa (nenhuma e perdida; a analise ja as registrou no estado dela). O push sai depois do commit, para todos os canais
(app e wokwi); o hub SSE guarda a mensagem para quem reconectar. A mensagem cujo prazo ja passou nao e enviada.

Estado do dispositivo: so grava quando a assinatura muda (`ResumoEstado.assinatura`).
"""
import threading
import time
from datetime import datetime
from typing import Callable

from borda import canal_app, canal_wokwi, tempo
from borda import const as const_borda
from comum import const as const_comum
from comum import mensagens
from comum.contrato_analise import Decisoes
from flow_db import repositorios
from flow_db.repo import Repo

CANAIS = [canal_app, canal_wokwi]       # todos expoem CANAL e enviar(user_id, mensagem)


class Aplicador:
    def __init__(self, obter_repo: Callable[[], Repo | None] = repositorios.repo,
                 relogio: Callable[[], datetime] = tempo.agora_utc, canais: list | None = None):
        self._obter_repo = obter_repo
        self._relogio = relogio
        self._canais = canais
        self._lock = threading.Lock()
        self._pendentes = Decisoes()              # decisoes que ainda nao chegaram ao banco
        self._assinaturas: dict = {}              # (user, device) -> assinatura do estado JA gravado
        self._ultimo_log_falha = float("-inf")

    @property
    def canais(self) -> list:
        return CANAIS if self._canais is None else self._canais

    @property
    def pendentes(self) -> int:
        return len(self._pendentes.comandos) + len(self._pendentes.alertas) + len(self._pendentes.confirmacoes)

    def aplicar(self, decisoes: Decisoes | None = None) -> bool:
        """Grava as pendentes (mais `decisoes`, se dada). Devolve True se nao ficou nada pendente. Nunca levanta."""
        with self._lock:
            if decisoes is not None:
                self._pendentes.somar(decisoes)
            if not self._pendentes:
                return True
            repo = self._obter_repo()
            if repo is None:
                return False
            novas: dict = {}
            try:
                with repo.transacao() as tx:
                    self._gravar(tx, self._pendentes, novas)
            except Exception as e:  # noqa: BLE001  (o banco nunca derruba a borda)
                self._falha(e)
                return False
            feitas, self._pendentes = self._pendentes, Decisoes()
            self._assinaturas.update(novas)
        self._depois_do_commit(feitas)
        return True

    # ---- banco ----
    def _gravar(self, tx: Repo, d: Decisoes, novas: dict) -> None:
        for c in d.confirmacoes:
            linha = {"timestamp": c.instante.isoformat(), "tipo": c.tipo, **c.dados}
            tx.gravar_confirmacao({"instante": c.instante, "user_id": c.user_id, "tipo": c.tipo, "dados": linha,
                                   "episodio_id": linha.get("episodeId"), "message_id": linha.get("messageId"),
                                   "schema_version": const_comum.SCHEMA_VERSION_MENSAGEM})
        for a in d.alertas:
            tx.gravar_alerta(a.como_linha())
        for r in d.estados:
            chave = (r.user_id, r.device_id)
            if novas.get(chave, self._assinaturas.get(chave)) == r.assinatura:
                continue
            tx.gravar_estado_dispositivo(r.como_linha())
            novas[chave] = r.assinatura
        for cmd in d.comandos:
            tx.gravar_episodio(cmd.episodio.como_linha())
            tx.gravar_comando(cmd.como_linha())

    def _falha(self, e: Exception) -> None:
        agora = time.monotonic()
        if agora - self._ultimo_log_falha >= const_borda.INTERVALO_LOG_FALHA_S:
            self._ultimo_log_falha = agora
            print(f"  [efeitos] banco indisponivel: decisoes guardadas, sem push, tenta de novo "
                  f"({self.pendentes} pendente(s)): {type(e).__name__}: {str(e)[:200]}")

    # ---- push ----
    def _depois_do_commit(self, d: Decisoes) -> None:
        for a in d.alertas:
            print(f"  >>> ALERTA gerado: {a.tipo} user={a.user_id} device={a.device_id} -> {a.dados.get('mensagem')}")
        agora = self._relogio()
        for cmd in d.comandos:
            try:
                self._entregar(cmd.user_id, cmd.mensagem, agora)
            except Exception as e:  # noqa: BLE001  (o push nao entregue nunca desfaz o que ja foi gravado)
                print(f"  [push] falha na entrega de {cmd.mensagem.get('messageType')}: {type(e).__name__}")

    def _entregar(self, user_id: int, msg: dict, agora: datetime) -> None:
        if mensagens.para_envio(msg, agora) is None:
            print(f"  [push] {msg['messageType']} user={user_id} -> EXPIRADO_ANTES_DA_ENTREGA")
            return
        for canal in self.canais:
            try:
                detalhe = canal.enviar(user_id, msg)
            except Exception as e:  # noqa: BLE001  (a falha de um canal nao impede os demais)
                detalhe = f"ERRO_NO_CANAL ({type(e).__name__}: {str(e)[:200]})"
            print(f"  [push] {msg['messageType']} user={user_id} canal={canal.CANAL} -> {detalhe}")
