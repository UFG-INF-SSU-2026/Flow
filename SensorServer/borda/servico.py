"""
A BORDA como processo proprio: `python -m borda` (a partir de SensorServer/).

Unico processo com porta de rede, com a chave AES, o segredo JWT, as contas e o banco; nao importa a analise (conferido
por tests/unit/test_fronteiras.py). Serve HTTPS (certificado e chave de comum/tls.py; sem eles nao sobe). Quem decide queda,
imobilidade e luz e o processo `python -m analise`, que a borda chama por HTTPS local (FLOW_ANALISE_URL, FLOW_ANALISE_TOKEN);
a borda grava as leituras e as decisoes, entrega o push e responde as consultas. A ordem de subida entre os dois processos e
indiferente.
(`python -m borda.server`, o modo de desenvolvimento, sobe a mesma borda sem o desligamento ordenado.)

Desligamento ordenado (Ctrl+C, SIGTERM, Ctrl+Break no Windows): para de aceitar conexoes, para as threads de fundo, fecha os
streams SSE, descarrega a fila de leituras e fecha o pool do banco (server.encerrar).
"""
import signal
import sys
import threading

from comum import const as const_comum
from comum import tls
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
    try:
        contexto = tls.contexto_servidor()     # antes de abrir banco e threads: sem certificado nada sobe
    except tls.ConfigTls as e:
        print(f"erro: {e}", file=sys.stderr)
        return CODIGO_ERRO
    try:
        from borda import server        # importar = inicializar: segredo JWT, banco, analise e threads de fundo
    except RuntimeError as e:
        print(f"erro: {e}", file=sys.stderr)
        return CODIGO_ERRO

    from werkzeug.serving import make_server
    try:
        srv = make_server("0.0.0.0", const_borda.PORT, server.app, threaded=True,   # threaded: uma thread por stream SSE
                          ssl_context=contexto)
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
    print(f"Borda iniciando em https://0.0.0.0:{const_borda.PORT} (a analise e outro processo: 'python -m analise').")
    print(f"Configuracao: {const_comum.DOTENV_ARQUIVO} "
          + ("carregado" if const_comum.DOTENV_CARREGADO else "nao encontrado; usando so as variaveis de ambiente"))
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
        server.encerrar()
    return 0
