"""
A analise e a entrega de push EM MEMORIA, no lugar dos dois processos (`python -m analise` e a thread de entrega da borda).

`Pipeline.drenar()` faz o que as threads de fundo fariam, de forma SINCRONA: descarrega a fila de leituras da borda,
roda um ciclo do laco da analise e o passo do vigia (que decidem e grava `comandos_push`, `episodios`, `confirmacoes`, `alertas` e
`estado_dispositivo`) e um ciclo da entrega da borda (que le `comandos_push` e entrega aos canais). O laco usa o
`nucleo.PADRAO` como estado: os testes que inspecionam `regra_queda._episodios` e afins continuam enxergando o estado.

O relogio e o de `borda.tempo.agora_utc` na hora do uso (a fixture `relogio` o congela). Falha de um ciclo e engolida e
logada, como nas threads reais.
"""
import threading

from analise import laco, nucleo
from borda import entrega_push, server, tempo


class Pipeline:
    def __init__(self):
        relogio = lambda: tempo.agora_utc()     # noqa: E731  (resolvido na hora do uso: a fixture `relogio` o troca)
        self.laco = laco.LacoAnalise(relogio=relogio, ao_decidir=lambda *_: None, agir=True, estado=nucleo.PADRAO)
        self.entrega = entrega_push.EntregaPush(relogio=relogio)
        self._lock = threading.Lock()

    def drenar(self, ciclos: int = 2) -> None:
        with self._lock:
            server._descarregar_leituras()
            for _ in range(ciclos):
                for ciclo in (self.laco.executar_ciclo, self.laco.passo_vigia, self.entrega.executar_ciclo):
                    try:
                        ciclo()
                    except Exception as e:  # noqa: BLE001  (a thread real registra e tenta de novo)
                        print(f"  [teste] falha no ciclo (ignorada): {type(e).__name__}: {str(e)[:150]}")
