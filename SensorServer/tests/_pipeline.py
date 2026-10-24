"""
A analise EM MEMORIA, no lugar do processo `python -m analise`: a borda do teste a chama direto (sem rede), pelo mesmo
`ClienteAnalise` que a producao usa, so que com outro transporte.

`Pipeline.drenar()` faz o que as threads de fundo fariam, de forma SINCRONA: grava a fila de leituras, manda a fila a analise,
aplica as decisoes (banco falso) e entrega o push, e roda o passo do vigia. A analise usa o `nucleo.PADRAO` como estado: os
testes que inspecionam `regra_queda._episodios` e afins continuam enxergando o estado.

`Pipeline.reiniciar_analise()` faz o papel de "derrubar a analise e subir outra": o estado em memoria some e o `boot_id`
muda; o banco fica. O relogio e o de `borda.tempo.agora_utc` na hora do uso (a fixture `relogio` o congela).
"""
import json
import threading

from analise import api, nucleo
from borda import cliente_analise, server, tempo


class Pipeline:
    def __init__(self, estado=None):
        self._lock = threading.Lock()
        self.fora_do_ar = False          # True: o transporte levanta, como uma analise parada
        self.chamadas: list[tuple] = []  # (metodo, caminho) de cada chamada que chegou a analise
        self.analise = None
        self.reiniciar_analise(estado if estado is not None else nucleo.PADRAO)
        server.configurar_analise(self._transporte).restaurar()   # contato inicial: estado limpo, boot_id conhecido

    def reiniciar_analise(self, estado=None) -> None:
        """Uma analise nova (estado vazio, outro boot_id). `estado=None` usa um estado novo e independente."""
        self.analise = api.Analise(relogio=lambda: tempo.agora_utc(),
                                   estado=estado if estado is not None else nucleo.novo_estado())

    def _transporte(self, metodo, caminho, corpo, timeout_s):
        if self.fora_do_ar:
            raise ConnectionError("analise fora do ar")
        self.chamadas.append((metodo, caminho))
        try:      # ida e volta por JSON, como no HTTP: o que nao for JSON aparece aqui, nao so em producao
            corpo = None if corpo is None else json.loads(json.dumps(corpo))
            return json.loads(json.dumps(self.analise.tratar(metodo, caminho, corpo), default=str))
        except api.RequisicaoInvalida:
            raise cliente_analise.RequisicaoRecusada(caminho) from None

    def drenar(self, ciclos: int = 2) -> None:
        with self._lock:
            for _ in range(ciclos):
                server.ciclo_pipeline()
                server.passo_vigia()
