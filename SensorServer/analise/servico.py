"""
A ANALISE como processo proprio (roadmap do split, S5): `python -m analise` (a partir de SensorServer/).

Sem porta de rede, sem chave AES, sem segredo JWT, sem contas. Le SO o `.env.analise` e o papel de banco flow_analise.
Roda o laco por cursor (analise/laco.py) com `agir=True`: le `leituras`, `respostas_dispositivo` e `entregas_push`, decide
e grava `comandos_push`, `alertas`, `confirmacoes`, `episodios` e `estado_dispositivo` na mesma transacao do cursor (DS5);
o vigia de episodios e um passo do laco. A borda (outro processo) entrega o push a partir de `comandos_push`.

Na subida (S6) o laco RECUPERA o estado das regras antes do primeiro ciclo: rele as leituras dos ultimos segundos ate o
cursor (sem agir), restaura os episodios abertos e recentes a partir do banco e so entao segue do cursor, agindo apenas
sobre leitura recente (analise/laco.py, `recuperar_estado`). FLOW_RECUPERAR_ESTADO=0 desliga.

Desligamento ordenado (Ctrl+C, SIGTERM, Ctrl+Break no Windows): o ciclo em curso termina e grava o cursor; depois o pool
do banco e fechado. Reiniciar a analise nao toca nos clientes SSE (eles estao na borda).

Este modulo nao importa nada de rede (conferido por tests/unit/test_dois_processos.py).
"""
import signal
import sys
import threading

from comum import const as const_comum
from comum import isolamento
from analise import const as const_analise
from analise import laco
from flow_db import repositorios

CODIGO_ERRO = 1


def _instalar_sinais(ao_receber) -> None:
    for nome in ("SIGINT", "SIGTERM", "SIGBREAK"):    # SIGBREAK so existe no Windows
        sinal = getattr(signal, nome, None)
        if sinal is not None:
            try:
                signal.signal(sinal, ao_receber)
            except (ValueError, OSError):
                pass


def main() -> int:
    if const_comum.SERVICO != "analise":
        print("erro: inicie a analise por 'python -m analise' (a partir de SensorServer/).", file=sys.stderr)
        return CODIGO_ERRO
    try:
        isolamento.exigir_ambiente("analise")
        repositorios.iniciar_analise()      # so o papel flow_analise; sem banco (ou esquema defasado) nao sobe
    except (isolamento.AmbienteInvalido, RuntimeError) as e:
        print(f"erro: {e}", file=sys.stderr)
        return CODIGO_ERRO

    parando = threading.Event()
    _instalar_sinais(lambda _sinal, _quadro: parando.set())

    analise = laco.LacoAnalise(agir=True)
    print("Analise iniciando (sem porta de rede; decide pelo banco e a borda entrega).")
    print(f"Configuracao: {const_comum.DOTENV_ARQUIVO} "
          + ("carregado" if const_comum.DOTENV_CARREGADO else "nao encontrado; usando so as variaveis de ambiente")
          + f" | versao do codigo: {const_analise.VERSAO_CODIGO_ACOES}"
          + f" | recuperacao na subida: {'sim' if const_analise.RECUPERAR_AO_INICIAR else 'nao'}")
    analise.iniciar()
    try:
        while not parando.wait(0.5):        # com timeout: no Windows um wait() sem prazo nao deixa o sinal chegar
            pass
        print("  [analise] sinal de encerramento: terminando o ciclo em curso...")
    finally:
        analise.parar(const_analise.PRAZO_ENCERRAMENTO_ANALISE_S)
        repositorios.desinstalar()
        print("  [analise] encerrada")
    return 0
