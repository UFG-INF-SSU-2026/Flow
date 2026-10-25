"""
TLS em todas as pontas: um certificado so, autoassinado, gerado por `python scripts/gerar_certificado.py`.

    certificado()         caminho do certificado (FLOW_TLS_CERT): o que o servidor apresenta e o que os clientes aceitam como
                          confianca (borda -> analise, borda -> banco, app, bridge)
    chave()               caminho da chave privada (FLOW_TLS_KEY): so quem serve HTTPS (borda, analise) e o container do banco
    contexto_servidor()   SSLContext para a borda e a analise servirem HTTPS
    exigir_https(url, v)  recusa uma URL que nao comece com https:// (nenhuma ponta fala em texto claro, nem em 127.0.0.1)

Caminho relativo vale a partir de SensorServer/, e nao de onde o processo foi iniciado. Os padroes (certs/flow.crt e
certs/flow.key) estao em comum/const.py. Escopo minimo: sem autoridade certificadora propria, sem rotacao automatica e sem
mTLS (o servidor nao pede certificado ao cliente; quem se autentica e a conta, por token).

Este modulo so le o ambiente na hora do uso e nao importa nada do projeto alem de `comum.const`.
"""
import os
import ssl
from pathlib import Path

from comum import const as const_comum


class ConfigTls(RuntimeError):
    """Certificado ou chave ausente ou ilegivel. A mensagem diz o que fazer e nunca traz o conteudo de um arquivo."""


_COMO_GERAR = "Gere o par com: python scripts/gerar_certificado.py (a partir de SensorServer/)."


def _caminho(variavel: str, padrao: str) -> Path:
    caminho = Path(os.environ.get(variavel) or padrao).expanduser()
    # A raiz e a pasta do .env (SensorServer/), a mesma de scripts/ e certs/.
    return caminho if caminho.is_absolute() else const_comum.DOTENV_ARQUIVO.parent / caminho


def _existente(variavel: str, padrao: str, o_que: str) -> str:
    caminho = _caminho(variavel, padrao)
    if not caminho.is_file():
        raise ConfigTls(f"{o_que} nao encontrado: {caminho}. {_COMO_GERAR}")
    return str(caminho)


def certificado() -> str:
    return _existente("FLOW_TLS_CERT", const_comum.TLS_CERT_PADRAO, "certificado TLS")


def chave() -> str:
    return _existente("FLOW_TLS_KEY", const_comum.TLS_CHAVE_PADRAO, "chave TLS")


def contexto_servidor() -> ssl.SSLContext:
    """Contexto para servir HTTPS (TLS 1.2 ou superior) com o certificado e a chave do projeto."""
    contexto = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    contexto.minimum_version = ssl.TLSVersion.TLSv1_2
    cert, chv = certificado(), chave()
    try:
        contexto.load_cert_chain(cert, chv)
    except (ssl.SSLError, OSError) as e:
        raise ConfigTls(f"nao foi possivel carregar o certificado e a chave TLS ({type(e).__name__}): o par nao confere "
                        f"ou o arquivo esta ilegivel. Recrie com: python scripts/gerar_certificado.py --forcar") from None
    return contexto


def exigir_https(url: str, variavel: str) -> str:
    """Devolve `url` se for https://; senao levanta ConfigTls citando a variavel que a trouxe."""
    if not url.lower().startswith("https://"):
        raise ConfigTls(f"{variavel} precisa comecar com https:// (toda ponta fala TLS); recebido: {url}")
    return url
