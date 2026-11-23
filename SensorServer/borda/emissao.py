"""
Emissao do certificado de cliente de um app ou dispositivo (logica pura, sem Flask): recebe o CSR que o proprio cliente gerou
(a chave privada nunca sai dele) e devolve um certificado assinado pela CA de dispositivos, que e independente da CA raiz.

A identidade do certificado vem SO de quem chama (conta e device_id do token), nunca do CSR. Do CSR o servidor usa apenas a
chave publica, depois de conferir que e EC P-256, que o CSR foi assinado com ECDSA e SHA-256 e que a assinatura confere (prova
de que o cliente tem a chave). Assunto e extensoes pedidos no CSR sao ignorados.

Tambem confere o certificado na volta (configurar_tls_de_cliente, identidade_do_cliente). Todo cliente com fonte (app e
wokwi) apresenta o seu; authz.py compara a identidade dele com a do token. Dois caminhos:

    app -> borda             o app apresenta o certificado no TLS da propria borda (CA de dispositivos)
    wokwi -> bridge -> borda o bridge termina o TLS do dispositivo e o confere; na conexao com a borda apresenta o PROPRIO
                             certificado (certs/bridge.crt, CA raiz, com clientAuth) e repassa o do dispositivo no cabecalho
                             CABECALHO_CERTIFICADO_REPASSADO (base64 do DER). A borda so aceita esse cabecalho numa conexao
                             autenticada como o bridge e confere de novo o certificado repassado (emissor, assinatura,
                             validade, clientAuth). Em qualquer outra conexao o cabecalho e ignorado.

Quem ainda nao tem certificado (login, refresh, emissao) passa sem ele. A borda nao sobe sem o certificado da CA de
dispositivos; sem a CHAVE dela so a emissao responde 503, e certificados ja emitidos continuam valendo.

A CA de dispositivos (certs/ca-dispositivos.crt e .key, gerada por scripts/gerar_certificado.py) e lida a cada uso, sem
cache. Caminhos: FLOW_CA_DISPOSITIVOS_CERT e FLOW_CA_DISPOSITIVOS_KEY, ou os padroes de borda/const.py; caminho relativo vale
a partir de SensorServer/. So a borda le a chave desta CA. Nenhuma mensagem de erro traz o conteudo do CSR, de certificado
nem de arquivo de CA.
"""
import base64
import binascii
import os
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from borda import const as const_borda
from comum import const as const_comum
from comum import tls


class CsrInvalido(Exception):
    """CSR recusado. `str(e)` diz so a categoria do problema, nunca o conteudo recebido."""


class EmissaoIndisponivel(Exception):
    """CA de dispositivos ausente, ilegivel, vencida ou incoerente. A mensagem nao traz conteudo de arquivo."""


@dataclass(frozen=True)
class Emitido:
    certificado_pem: str
    serial: str                 # hexadecimal
    valido_ate: datetime        # UTC


_COMO_GERAR = "Gere a CA de dispositivos com: python scripts/gerar_certificado.py (a partir de SensorServer/)."


def _caminho(variavel: str, padrao: str) -> Path:
    caminho = Path(os.environ.get(variavel) or padrao).expanduser()
    # Mesma regra de comum/tls.py: relativo vale a partir da pasta do .env (SensorServer/).
    return caminho if caminho.is_absolute() else const_comum.DOTENV_ARQUIVO.parent / caminho


def configurar_tls_de_cliente(contexto: ssl.SSLContext) -> None:
    """Faz o servidor TLS pedir certificado de cliente, opcional, e confiar em duas CAs: a de dispositivos (app e, no bridge,
    o dispositivo) e a raiz (so o certificado do bridge tem `clientAuth` emitido por ela). Certificado de outra CA, vencido
    ou sem `clientAuth` tem o handshake recusado; quem nao apresenta nenhum conecta (e a rota decide, ver authz.py).
    Levanta EmissaoIndisponivel se a CA de dispositivos falta ou esta ilegivel: a borda nao sobe sem ela."""
    cert = _caminho("FLOW_CA_DISPOSITIVOS_CERT", const_borda.CA_DISPOSITIVOS_CERT_PADRAO)
    if not cert.is_file():
        raise EmissaoIndisponivel(f"CA de dispositivos nao encontrada; sem ela a borda nao confere os clientes. {_COMO_GERAR}")
    try:
        contexto.load_verify_locations(cafile=str(cert))
    except (ssl.SSLError, OSError):
        raise EmissaoIndisponivel(f"CA de dispositivos ilegivel. {_COMO_GERAR}") from None
    contexto.load_verify_locations(cafile=tls.ca())       # a raiz: o certificado de cliente do bridge
    contexto.verify_mode = ssl.CERT_OPTIONAL


def _ler_certificado(caminho: Path) -> x509.Certificate | None:
    try:
        return x509.load_pem_x509_certificate(caminho.read_bytes())
    except (ValueError, OSError):
        return None


def _do_bridge(cert: x509.Certificate) -> bool:
    """O certificado (ja validado pelo TLS) e o do bridge: emitido pela CA raiz, com o nome reservado do bridge."""
    raiz = _ler_certificado(_caminho("FLOW_TLS_CA", const_comum.TLS_CA_PADRAO))
    nomes = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return (raiz is not None and cert.issuer == raiz.subject
            and [n.value for n in nomes] == [const_borda.NOME_CERTIFICADO_BRIDGE])


def _repassado(valor, ca: x509.Certificate, agora: datetime) -> x509.Certificate | None:
    """O certificado do dispositivo que o bridge repassou, se for da CA de dispositivos, valido agora e com `clientAuth`."""
    try:
        cert = x509.load_der_x509_certificate(base64.b64decode(valor, validate=True))
        cert.verify_directly_issued_by(ca)                    # emissor e assinatura
        usos = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except (ValueError, TypeError, binascii.Error, InvalidSignature, UnsupportedAlgorithm, x509.ExtensionNotFound):
        return None
    if ExtendedKeyUsageOID.CLIENT_AUTH not in usos or not cert.not_valid_before_utc <= agora <= cert.not_valid_after_utc:
        return None
    return cert


def identidade_do_cliente(environ, agora: datetime) -> tuple[str, str] | None:
    """(conta, device_id) do cliente desta conexao, ou None se nao houver um certificado de dispositivo valido.

    Conexao com certificado da CA de dispositivos: a identidade e a dele (o TLS ja o validou). Conexao do bridge: a
    identidade e a do certificado repassado no cabecalho, conferido aqui. Qualquer outro caso (sem TLS, sem certificado,
    outro emissor, bridge sem cabecalho ou com um invalido): None. Le o `OU` e o `CN` que emitir_certificado grava."""
    pegar = getattr(environ.get("werkzeug.socket"), "getpeercert", None)
    if pegar is None:                                         # sem TLS (teste, desenvolvimento)
        return None
    try:
        der = pegar(binary_form=True)
    except ValueError:                                        # handshake ainda nao feito ou conexao sem TLS
        return None
    ca = _ler_certificado(_caminho("FLOW_CA_DISPOSITIVOS_CERT", const_borda.CA_DISPOSITIVOS_CERT_PADRAO))
    if not der or ca is None:
        return None
    cert = x509.load_der_x509_certificate(der)
    if _do_bridge(cert):
        valor = environ.get("HTTP_" + const_borda.CABECALHO_CERTIFICADO_REPASSADO.upper().replace("-", "_"))
        cert = _repassado(valor, ca, agora) if valor else None
    elif cert.issuer != ca.subject:
        cert = None
    if cert is None:
        return None
    conta = [a.value for a in cert.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)]
    device = [a.value for a in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)]
    return (conta[0], device[0]) if len(conta) == 1 and len(device) == 1 and conta[0] and device[0] else None


def _carregar_ca(agora: datetime) -> tuple[x509.Certificate, object]:
    """(certificado, chave privada) da CA de dispositivos, conferidos entre si e com a validade."""
    cert_arq = _caminho("FLOW_CA_DISPOSITIVOS_CERT", const_borda.CA_DISPOSITIVOS_CERT_PADRAO)
    chave_arq = _caminho("FLOW_CA_DISPOSITIVOS_KEY", const_borda.CA_DISPOSITIVOS_CHAVE_PADRAO)
    if not cert_arq.is_file() or not chave_arq.is_file():
        raise EmissaoIndisponivel(f"CA de dispositivos nao encontrada. {_COMO_GERAR}")
    try:
        ca = x509.load_pem_x509_certificate(cert_arq.read_bytes())
        chave = serialization.load_pem_private_key(chave_arq.read_bytes(), password=None)
    except (ValueError, TypeError, OSError):
        raise EmissaoIndisponivel(f"CA de dispositivos ilegivel. {_COMO_GERAR}") from None
    publica = chave.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    da_ca = ca.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if publica != da_ca:
        raise EmissaoIndisponivel(f"a chave da CA de dispositivos nao corresponde ao certificado. {_COMO_GERAR}")
    if not ca.not_valid_before_utc <= agora <= ca.not_valid_after_utc:
        raise EmissaoIndisponivel(f"a CA de dispositivos esta fora da validade. {_COMO_GERAR}")
    return ca, chave


def _ler_csr(texto) -> x509.CertificateSigningRequest:
    """Aceita so um bloco PEM de pedido de certificado, EC P-256, ECDSA com SHA-256, com assinatura valida."""
    if not isinstance(texto, str):
        raise CsrInvalido("csr nao e texto")
    pem = texto.encode("utf-8", errors="replace")
    if pem.count(b"-----BEGIN ") != 1:
        raise CsrInvalido("csr deve ter exatamente um bloco PEM")
    try:
        csr = x509.load_pem_x509_csr(pem)
    except (ValueError, TypeError):
        raise CsrInvalido("csr nao e um pedido de certificado PEM valido") from None
    try:
        chave = csr.public_key()
    except (ValueError, UnsupportedAlgorithm):
        raise CsrInvalido("a chave do csr deve ser EC na curva P-256") from None
    if not isinstance(chave, ec.EllipticCurvePublicKey) or not isinstance(chave.curve, ec.SECP256R1):
        raise CsrInvalido("a chave do csr deve ser EC na curva P-256")
    if not isinstance(csr.signature_hash_algorithm, hashes.SHA256):
        raise CsrInvalido("o csr deve ser assinado com ECDSA e SHA-256")
    try:
        chave.verify(csr.signature, csr.tbs_certrequest_bytes, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise CsrInvalido("assinatura do csr nao confere") from None
    return csr


def emitir_certificado(csr_texto, conta_id: str, device_id: str, agora: datetime) -> Emitido:
    """Valida o CSR e emite o certificado de cliente do dispositivo `device_id` da conta `conta_id`.

    `agora` e o instante da emissao (UTC; a rota passa o real). Levanta CsrInvalido (CSR recusado ou identificacao vazia ou
    longa demais para o assunto) ou EmissaoIndisponivel (CA de dispositivos)."""
    if not conta_id or not device_id:
        raise CsrInvalido("identificacao do dispositivo ausente")
    agora = agora.replace(microsecond=0)          # o certificado guarda segundos: o log diz o que esta nele
    csr = _ler_csr(csr_texto)
    ca, chave_ca = _carregar_ca(agora)

    fim = min(agora + timedelta(days=const_borda.CERT_DISPOSITIVO_DIAS), ca.not_valid_after_utc)   # nunca vale mais que a CA
    try:
        assunto = x509.Name([x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, conta_id),
                             x509.NameAttribute(NameOID.COMMON_NAME, device_id)])
    except ValueError:
        raise CsrInvalido("identificacao do dispositivo fora do formato aceito") from None
    certificado = (
        x509.CertificateBuilder()
        .subject_name(assunto).issuer_name(ca.subject)
        .public_key(csr.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(agora - timedelta(minutes=5))          # folga para relogios um pouco atrasados (como no script)
        .not_valid_after(fim)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, key_cert_sign=False,
                                     content_commitment=False, data_encipherment=False, key_agreement=False,
                                     crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(csr.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()), critical=False)
        .sign(chave_ca, hashes.SHA256())
    )
    return Emitido(certificado.public_bytes(serialization.Encoding.PEM).decode("ascii"),
                   format(certificado.serial_number, "x"), fim)
