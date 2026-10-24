"""
Entrega do push (roadmap do split, S4/S7): a BORDA le `comandos_push` por cursor, entrega por SSE e grava
`entregas_push` e o cursor da borda (`cursor_borda`). E o unico caminho do push: a analise decide e grava o comando, e
quem fala com os dispositivos e a borda.

Cada comando vai a TODOS os canais (app e wokwi) e cada canal gera uma linha de `entregas_push` (com `comando_id`):
`enviado`, `sem_conexao` (ninguem conectado; o hub ainda guarda a mensagem para o replay de quem reconectar),
`nao_suportado`, `erro` ou `expirado` (o prazo do comando passou antes da entrega: nada e enviado, e a analise ve o
resultado). Comando criado ha mais que const_borda.JANELA_COMANDOS_S nem e lido (historico de antes de uma parada). A
borda nao importa a analise: o contrato entre as duas e a tabela.

Garantias: entrega ao menos uma vez (se a borda cair entre entregar e gravar o cursor, o comando sai de novo; o
dispositivo ignora o `messageId` repetido e o prazo limita o reenvio). Falha do banco ou de um canal nunca derruba a
thread: o ciclo e refeito no proximo passo, e uma linha de log por const_borda.INTERVALO_LOG_FALHA_ENTREGA_S.
"""
import threading
import time
from datetime import datetime, timedelta
from typing import Callable

from borda import canal_app, canal_wokwi, tempo
from borda import const as const_borda
from comum import mensagens
from flow_db import repositorios
from flow_db.repo_borda import RepoBorda

# Canais de push (servidor -> dispositivo). Todos expoem CANAL e enviar(user_id, mensagem).
CANAIS = [canal_app, canal_wokwi]

RESULTADO_ERRO_NO_CANAL = "ERRO_NO_CANAL"   # canal.enviar levantou excecao (alem de ENTREGUE_AO_CANAL etc. dos canais)

# Resultado do canal -> valor gravado em `entregas_push.resultado`.
RESULTADO_ENTREGA = {"ENTREGUE_AO_CANAL": "enviado", "SEM_CANAL_ATIVO": "sem_conexao",
                     "TIPO_NAO_SUPORTADO": "nao_suportado", RESULTADO_ERRO_NO_CANAL: "erro"}


class EntregaPush:
    """`executar_ciclo()` e SINCRONO (o que os testes chamam); `iniciar()` sobe a thread que o repete.

    Parametros (opcionais; o padrao e o servidor real): `obter_repo` (devolve o RepoBorda ou None), `relogio`
    (datetime UTC), `canais` (lista com CANAL e enviar)."""

    def __init__(self, obter_repo: Callable[[], RepoBorda | None] = repositorios.borda,
                 relogio: Callable[[], datetime] = tempo.agora_utc, canais: list | None = None):
        self._obter_repo = obter_repo
        self._relogio = relogio
        self._canais = canais
        self._parar = threading.Event()
        self._thread: threading.Thread | None = None
        self._ultimo_log_falha = float("-inf")

    @property
    def canais(self) -> list:
        return CANAIS if self._canais is None else self._canais

    # ---- um ciclo ----
    def executar_ciclo(self) -> int:
        """Le os comandos novos, entrega cada um e avanca o cursor. Devolve quantos comandos leu (0 sem banco).
        Excecao do banco SOBE (a thread registra e tenta de novo; o cursor so avanca depois de entregar)."""
        repo = self._obter_repo()
        if repo is None:
            return 0
        agora = self._relogio()
        cursor = repo.ler_cursor_borda()
        minimo = agora - timedelta(seconds=const_borda.JANELA_COMANDOS_S)
        comandos = repo.ler_comandos_novos(cursor["ultimo_comando_id"], minimo, const_borda.LOTE_COMANDOS)
        if not comandos:
            return 0
        for comando in comandos:
            self._entregar(repo, comando, agora)
        repo.gravar_cursor_borda(comandos[-1]["id"], comandos[-1]["criado_em"])
        return len(comandos)

    def _entregar(self, repo: RepoBorda, comando: dict, agora: datetime) -> None:
        msg = comando["payload"]
        expirado = mensagens.para_envio(msg, agora) is None
        for canal in self.canais:
            if expirado:
                resultado, detalhe = "expirado", "EXPIRADO_ANTES_DA_ENTREGA"
            else:
                try:
                    detalhe = canal.enviar(comando["user_id"], msg)
                except Exception as e:  # noqa: BLE001  (a falha de um canal nao impede os demais)
                    detalhe = RESULTADO_ERRO_NO_CANAL
                    print(f"  [push] falha no canal={canal.CANAL} user={comando['user_id']}: "
                          f"{type(e).__name__}: {str(e)[:200]}")
                resultado = RESULTADO_ENTREGA.get(detalhe, "erro")
            linha = {"instante": agora, "comando_id": comando["id"], "message_id": comando["message_id"],
                     "user_id": comando["user_id"], "device_id": None, "canal": canal.CANAL,
                     "resultado": resultado, "detalhe": detalhe}
            try:
                repo.gravar_entrega(linha)
            except Exception as e:  # noqa: BLE001  (a entrega ja foi feita: so a evidencia se perde, no log)
                print(f"  [push] falha ao gravar a entrega canal={canal.CANAL}: {type(e).__name__}: {str(e)[:200]}")
            print(f"  [push] {msg['messageType']} user={comando['user_id']} canal={canal.CANAL} -> {detalhe}")

    # ---- thread ----
    def _falha(self, e: Exception) -> None:
        agora = time.monotonic()
        if agora - self._ultimo_log_falha >= const_borda.INTERVALO_LOG_FALHA_ENTREGA_S:
            self._ultimo_log_falha = agora
            print(f"  [push] falha no ciclo de entrega (cursor nao avancou; tenta de novo): "
                  f"{type(e).__name__}: {str(e)[:200]}")

    def _repetir(self) -> None:
        while not self._parar.is_set():
            cheio = False
            try:
                cheio = self.executar_ciclo() >= const_borda.LOTE_COMANDOS     # lote cheio = ha mais: nao espera
            except Exception as e:  # noqa: BLE001  (a thread nunca pode morrer em silencio)
                self._falha(e)
            if not cheio:
                self._parar.wait(const_borda.INTERVALO_LEITURA_COMANDOS_S)

    def iniciar(self) -> threading.Thread:
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._parar.clear()
        self._thread = threading.Thread(target=self._repetir, name="flow-entrega-push", daemon=True)
        self._thread.start()
        return self._thread

    def parar(self, timeout: float = 5.0) -> None:
        self._parar.set()
        if self._thread is not None:
            self._thread.join(timeout)
