"""
Emissao do certificado de cliente de um app ou dispositivo (logica pura, sem Flask): recebe o CSR que o proprio cliente gerou
(a chave privada nunca sai dele) e devolve um certificado assinado pela CA de dispositivos, que e independente da CA raiz.

A identidade do certificado vem SO de quem chama (conta e device_id do token), nunca do CSR. Do CSR o servidor usa apenas a
chave publica, depois de conferir que e EC P-256, que o CSR foi assinado com ECDSA e SHA-256 e que a assinatura confere (prova
de que o cliente tem a chave). Assunto e extensoes pedidos no CSR sao ignorados.

Tambem confere o certificado na volta: na subida a borda pede certificado de cliente (opcional) e confia so na CA de
dispositivos (pedir_certificado_de_cliente); o TLS valida cadeia, validade e `clientAuth`, e authz.py compara a identidade
do certificado (identidade_do_cliente) com a do token. Quem ainda nao tem certificado (login, refresh, emissao) passa sem ele.

A CA de dispositivos (certs/ca-dispositivos.crt e .key, gerada por scripts/gerar_certificado.py) e lida a cada emissao, sem
cache: a rota e rara (um pedido por partida do dispositivo). Caminhos: FLOW_CA_DISPOSITIVOS_CERT e FLOW_CA_DISPOSITIVOS_KEY, ou
os padroes de borda/const.py; caminho relativo vale a partir de SensorServer/. So a borda le a chave desta CA. O servidor nao
exige o certificado so do app, que fala direto com a borda; o do dispositivo (wokwi) e conferido pelo bridge.
Nenhuma mensagem de erro traz o conteudo do CSR nem de arquivo de CA.
"""
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


def pedir_certificado_de_cliente(contexto: ssl.SSLContext) -> bool:
    """Faz o servidor TLS pedir certificado de cliente, opcional, e confiar so na CA de dispositivos. Quem apresenta um
    certificado que nao e dela, vencido ou sem `clientAuth` tem o handshake recusado; quem nao apresenta nenhum conecta
    (e a rota decide, ver authz.py). Devolve False, sem mexer no contexto, se a CA de dispositivos nao existe; levanta
    EmissaoIndisponivel se existe e esta ilegivel."""
    cert = _caminho("FLOW_CA_DISPOSITIVOS_CERT", const_borda.CA_DISPOSITIVOS_CERT_PADRAO)
    if not cert.is_file():
        return False
    try:
        contexto.load_verify_locations(cafile=str(cert))
    except (ssl.SSLError, OSError):
        raise EmissaoIndisponivel(f"CA de dispositivos ilegivel. {_COMO_GERAR}") from None
    contexto.verify_mode = ssl.CERT_OPTIONAL
    return True


def identidade_do_cliente(environ) -> tuple[str, str] | None:
    """(conta, device_id) do certificado de cliente que o TLS DESTA conexao ja validou, ou None se ela nao trouxe um. Le o
    `OU` e o `CN` que emitir_certificado grava (conta e device_id do token). Sem TLS (teste, desenvolvimento) devolve None."""
    pegar = getattr(environ.get("werkzeug.socket"), "getpeercert", None)
    if pegar is None:
        return None
    try:
        certificado = pegar() or {}
    except ValueError:                        # handshake ainda nao feito ou conexao sem TLS
        return None
    campos = {nome: valor for rdn in certificado.get("subject", ()) for nome, valor in rdn}
    conta, device_id = campos.get("organizationalUnitName"), campos.get("commonName")
    return (conta, device_id) if conta and device_id else None


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
