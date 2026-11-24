"""PKI de teste, so com `cryptography`: CA, certificado de servidor e certificados de cliente de varios tipos (inclusive vencido,
so serverAuth e sem ExtendedKeyUsage). Tudo em pasta temporaria; nunca em arquivo versionado."""
import ipaddress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

HOST = "127.0.0.1"


@dataclass
class Ca:
    cert: x509.Certificate
    chave: object
    arquivo: Path


def _nome(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _gravar(pasta: Path, nome: str, cert: x509.Certificate, chave=None) -> tuple[str, str | None]:
    c = pasta / f"{nome}.crt"
    c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k = None
    if chave is not None:
        k = pasta / f"{nome}.key"
        k.write_bytes(chave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    return str(c), (str(k) if k else None)


def criar_ca(pasta: Path, nome: str) -> Ca:
    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    agora = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(_nome(nome)).issuer_name(_nome(nome)).public_key(chave.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(agora - timedelta(minutes=5))
            .not_valid_after(agora + timedelta(days=2))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=False, key_encipherment=False, key_cert_sign=True, content_commitment=False,
                                         data_encipherment=False, key_agreement=False, crl_sign=True, encipher_only=False,
                                         decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(chave.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(chave.public_key()), critical=False)
            .sign(chave, hashes.SHA256()))
    arquivo, _ = _gravar(pasta, nome, cert)
    return Ca(cert, chave, Path(arquivo))


def _emitir(ca: Ca, cn: str, chave_publica, usos, valido_ate_dias: float, san=None):
    agora = datetime.now(timezone.utc)
    inicio, fim = ((agora - timedelta(days=3), agora - timedelta(days=1)) if valido_ate_dias < 0
                   else (agora - timedelta(minutes=5), agora + timedelta(days=valido_ate_dias)))
    b = (x509.CertificateBuilder().subject_name(_nome(cn)).issuer_name(ca.cert.subject).public_key(chave_publica)
         .serial_number(x509.random_serial_number()).not_valid_before(inicio).not_valid_after(fim)
         .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
         .add_extension(x509.SubjectKeyIdentifier.from_public_key(chave_publica), critical=False)
         .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.cert.public_key()), critical=False))
    if usos is not None:
        b = b.add_extension(x509.ExtendedKeyUsage(usos), critical=False)
    if san:
        b = b.add_extension(x509.SubjectAlternativeName(san), critical=False)
    return b.sign(ca.chave, hashes.SHA256())


def certificado_de_servidor(pasta: Path, ca: Ca, nome: str = "bridge", cliente: bool = True) -> tuple[str, str]:
    """O que `gerar_certificado.py --nome bridge` emite: serverAuth (serve a API) e clientAuth (identidade na borda), CN `bridge`."""
    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    usos = [ExtendedKeyUsageOID.SERVER_AUTH] + ([ExtendedKeyUsageOID.CLIENT_AUTH] if cliente else [])
    cert = _emitir(ca, nome, chave.public_key(), usos, 2,
                   san=[x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address(HOST))])
    return _gravar(pasta, nome, cert, chave)


def certificado_de_cliente(pasta: Path, ca: Ca, nome: str = "cliente", usos="clientAuth", dias: float = 1) -> tuple[str, str]:
    """`usos`: "clientAuth", "serverAuth" ou None (sem ExtendedKeyUsage). `dias` negativo = ja vencido. Chave EC P-256 (a do firmware)."""
    chave = ec.generate_private_key(ec.SECP256R1())
    lista = {"clientAuth": [ExtendedKeyUsageOID.CLIENT_AUTH], "serverAuth": [ExtendedKeyUsageOID.SERVER_AUTH], None: None}[usos]
    return _gravar(pasta, nome, _emitir(ca, nome, chave.public_key(), lista, dias), chave)


@dataclass
class Pki:
    pasta: Path
    ca_raiz: Ca
    ca_disp: Ca
    servidor_cert: str
    servidor_chave: str


def criar_pki(pasta: Path) -> Pki:
    raiz = criar_ca(pasta, "ca-raiz")
    disp = criar_ca(pasta, "ca-dispositivos")
    cert, chave = certificado_de_servidor(pasta, raiz)
    return Pki(pasta, raiz, disp, cert, chave)
