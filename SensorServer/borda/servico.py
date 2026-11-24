"""
A BORDA como processo proprio: `python -m borda` (a partir de SensorServer/).

Unico processo com porta de rede, com a chave AES, o segredo JWT, as contas e o banco; nao importa a analise (conferido
por tests/unit/test_fronteiras.py). Serve so HTTPS, em FLOW_BORDA_HOST:FLOW_BORDA_PORT (padrao 0.0.0.0:5000, todas as interfaces), com o certificado e a chave da borda
(comum/tls.py; sem eles nao sobe). Quem decide queda,
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
from pathlib import Path

from comum import const as const_comum
from comum import tls
from borda import const as const_borda
from borda import emissao

CODIGO_ERRO = 1


def _instalar_sinais(ao_receber) -> None:
    for nome in ("SIGINT", "SIGTERM", "SIGBREAK"):    # SIGBREAK so existe no Windows
        sinal = getattr(signal, nome, None)
        if sinal is not None:
            try:
                signal.signal(sinal, ao_receber)
            except (ValueError, OSError):             # fora da thread principal ou sinal nao suportado
                pass


def enderecos_de_rede(certificado: str) -> list[str]:
    """IPs fora do loopback que o certificado da borda lista: os enderecos que o app pode usar (outro, ele recusa)."""
    import ipaddress

    from cryptography import x509

    try:
        cert = x509.load_pem_x509_certificate(Path(certificado).read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except (ValueError, OSError, x509.ExtensionNotFound):
        return []
    return [str(ip) for ip in san.get_values_for_type(x509.IPAddress) if not ipaddress.ip_address(ip).is_loopback]


def main() -> int:
    if const_comum.SERVICO != "borda":
        print("erro: inicie a borda por 'python -m borda' (a partir de SensorServer/).", file=sys.stderr)
        return CODIGO_ERRO
    try:
        contexto = tls.contexto_servidor()     # antes de abrir banco e threads: sem certificado nada sobe
        emissao.configurar_tls_de_cliente(contexto)    # sem a CA de dispositivos a borda nao sobe
    except (tls.ConfigTls, emissao.EmissaoIndisponivel) as e:
        print(f"erro: {e}", file=sys.stderr)
        return CODIGO_ERRO
    try:
        from borda import server        # importar = inicializar: segredo JWT, banco, analise e threads de fundo
    except RuntimeError as e:
        print(f"erro: {e}", file=sys.stderr)
        return CODIGO_ERRO

    from werkzeug.serving import make_server
    try:
        srv = make_server(const_borda.HOST, const_borda.PORT, server.app, threaded=True,   # threaded: uma thread por stream SSE
                          ssl_context=contexto)
    except OSError as e:
        print(f"erro: nao foi possivel abrir {const_borda.HOST}:{const_borda.PORT}: {e}", file=sys.stderr)
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
    print(f"Borda iniciando em https://{const_borda.HOST}:{const_borda.PORT} (a analise e outro processo: 'python -m analise').")
    if const_borda.HOST == "0.0.0.0":
        ips = enderecos_de_rede(tls.certificado())
        print(f"No app: IP {' ou '.join(ips)}, porta {const_borda.PORT}" if ips
              else "aviso: nenhum IP de rede no certificado da borda; o celular nao conectara (rode scripts/gerar_certificado.py)")
    print("Certificado de cliente: exigido do app e do wokwi (este pelo bridge)")
    print(f"Configuracao: {const_comum.DOTENV_ARQUIVO} "
          + ("carregado" if const_comum.DOTENV_CARREGADO else "nao encontrado; usando so as variaveis de ambiente"))
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
        server.encerrar()
    return 0
