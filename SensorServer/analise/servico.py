"""
A ANALISE como processo proprio: `python -m analise` (a partir de SensorServer/).

Servidor 2: uma API HTTPS escutando SO em 127.0.0.1 (analise/api.py), sem banco, sem chave AES, sem segredo JWT e sem
contas. Do .env le apenas as variaveis FLOW_ANALISE_* (FLOW_ANALISE_TOKEN, obrigatoria, e FLOW_ANALISE_PORT) e FLOW_TLS_*
(caminhos do certificado e da chave, que comum/tls.py exige para servir HTTPS). Guarda o estado das regras em memoria; ao
reiniciar, a borda percebe o `boot_id` novo e manda o estado de volta (/restaurar).

Desligamento ordenado (Ctrl+C, SIGTERM, Ctrl+Break no Windows): para de aceitar conexoes e sai.
"""
import signal
import sys
import threading

from comum import const as const_comum
from comum import tls

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
        token = const_comum.token_analise()
        contexto = tls.contexto_servidor()      # ConfigTls e um RuntimeError: sem certificado a analise nao sobe
    except RuntimeError as e:
        print(f"erro: {e}", file=sys.stderr)
        return CODIGO_ERRO

    from werkzeug.serving import make_server
    from analise import api
    app = api.criar_app(api.Analise(), token)
    try:
        srv = make_server(const_comum.ANALISE_HOST, const_comum.ANALISE_PORT, app, threaded=True, ssl_context=contexto)
    except OSError as e:
        print(f"erro: nao foi possivel abrir {const_comum.ANALISE_HOST}:{const_comum.ANALISE_PORT}: {e}", file=sys.stderr)
        return CODIGO_ERRO

    parando = threading.Event()

    def pedir_parada(_sinal, _quadro):
        if parando.is_set():
            return
        parando.set()
        print("  [analise] sinal de encerramento: parando...")
        threading.Thread(target=srv.shutdown, name="flow-parada", daemon=True).start()

    _instalar_sinais(pedir_parada)
    print(f"Analise escutando em https://{const_comum.ANALISE_HOST}:{const_comum.ANALISE_PORT} (so a borda a chama).")
    print(f"Configuracao: {const_comum.DOTENV_ARQUIVO} "
          + ("carregado (so FLOW_ANALISE_* e FLOW_TLS_*)" if const_comum.DOTENV_CARREGADO
             else "nao encontrado; usando so as variaveis de ambiente"))
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
        print("  [analise] encerrada")
    return 0
