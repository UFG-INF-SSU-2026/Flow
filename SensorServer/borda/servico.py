"""
A BORDA como processo proprio (roadmap do split, S5): `python -m borda` (a partir de SensorServer/).

Unico processo com porta de rede, com a chave AES, o segredo JWT e as contas. Le SO o `.env.borda` e o papel de banco
flow_borda; nao importa a analise (conferido por tests/unit/test_fronteiras.py e tests/unit/test_dois_processos.py).
Quem decide queda, imobilidade e luz e o processo `python -m analise`; a borda grava as leituras, entrega o push que a
analise gravar em `comandos_push` e responde as consultas. A ordem de subida entre os dois processos e indiferente.
(`python -m borda.server`, o modo de desenvolvimento, sobe a mesma borda com o `.env` de sempre e sem o desligamento ordenado.)

Desligamento ordenado (Ctrl+C, SIGTERM, Ctrl+Break no Windows): para de aceitar conexoes, para a entrega, fecha os
streams SSE, descarrega a fila de leituras e o buffer e fecha as conexoes do pool (server.encerrar).
"""
import os
import signal
import sys
import threading

from comum import const as const_comum
from comum import isolamento
from borda import const as const_borda

CODIGO_ERRO = 1


def _instalar_sinais(ao_receber) -> None:
    for nome in ("SIGINT", "SIGTERM", "SIGBREAK"):    # SIGBREAK so existe no Windows
        sinal = getattr(signal, nome, None)
        if sinal is not None:
            try:
                signal.signal(sinal, ao_receber)
            except (ValueError, OSError):             # fora da thread principal ou sinal nao suportado
                pass


def main() -> int:
    if const_comum.SERVICO != "borda":
        print("erro: inicie a borda por 'python -m borda' (a partir de SensorServer/).", file=sys.stderr)
        return CODIGO_ERRO
    if os.environ.get("FLOW_DB_FAKE") == "1":
        print("erro: FLOW_DB_FAKE=1 nao vale para 'python -m borda': o banco falso vive na memoria de um processo e a "
              "analise e outro processo. Use o PostgreSQL (scripts/preparar).", file=sys.stderr)
        return CODIGO_ERRO
    try:
        isolamento.exigir_ambiente("borda")
        from borda import server        # importar = inicializar: segredo JWT, banco (so flow_borda) e threads de fundo
    except (isolamento.AmbienteInvalido, RuntimeError) as e:
        print(f"erro: {e}", file=sys.stderr)
        return CODIGO_ERRO

    from werkzeug.serving import make_server
    try:
        srv = make_server("0.0.0.0", const_borda.PORT, server.app, threaded=True)   # threaded: uma thread por stream SSE
    except OSError as e:
        print(f"erro: nao foi possivel abrir a porta {const_borda.PORT}: {e}", file=sys.stderr)
        server.encerrar()
        return CODIGO_ERRO

    parando = threading.Event()

    def pedir_parada(_sinal, _quadro):
        # srv.shutdown() espera serve_forever sair; chamado aqui (thread principal, dentro de serve_forever) travaria.
        if parando.is_set():
            return
        parando.set()
        print("  [borda] sinal de encerramento: parando de aceitar conexoes...")
        threading.Thread(target=srv.shutdown, name="flow-parada", daemon=True).start()

    _instalar_sinais(pedir_parada)
    print(f"Borda iniciando na porta {const_borda.PORT} (a analise e outro processo: 'python -m analise').")
    print(f"Configuracao: {const_comum.DOTENV_ARQUIVO} "
          + ("carregado" if const_comum.DOTENV_CARREGADO else "nao encontrado; usando so as variaveis de ambiente"))
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
        server.encerrar()
    return 0
