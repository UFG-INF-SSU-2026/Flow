#!/usr/bin/env python3
"""
Gera o certificado TLS UNICO do Flow (autoassinado) e a chave dele. Roda de qualquer pasta; so precisa do pacote
`cryptography` (ja esta em requirements.txt).

    python scripts/gerar_certificado.py                      cria se faltar; se o par ja existe, nao mexe
    python scripts/gerar_certificado.py --ip 192.168.0.10    inclui o IP do computador na rede (o que o celular usa)
    python scripts/gerar_certificado.py --forcar --ip ...    recria o par (p.ex. para trocar ou acrescentar um IP)

Arquivos:
    SensorServer/certs/flow.crt   certificado (publico): servido pela borda, pela analise e pelo banco; e a confianca de todos
                                  os clientes (borda -> analise, borda -> banco, bridge, app)
    SensorServer/certs/flow.key   chave privada (SEGREDO): so de quem serve. Nunca vai para o Git
    SensorApp/app/src/main/res/raw/flow_cert.pem   copia do certificado (publico) que o app embute como unica confianca;
                                  gere ANTES de compilar o app, e recompile se o certificado for recriado

O certificado vale para os nomes e IPs listados nele (SAN): localhost, 127.0.0.1, ::1, o IP do computador na rede (detectado) e
os que vierem em --ip/--dns. Um cliente que acesse o servidor por um endereco fora da lista recusa a conexao: nesse caso
recrie com --forcar e o endereco novo. Sem autoridade certificadora propria, sem rotacao automatica e sem mTLS.

Depois de recriar: `docker compose restart db` (o banco carrega o par ao subir) e reinicie borda e analise.
"""
import argparse
import ipaddress
import os
import shutil
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent                   # SensorServer/
CERT = RAIZ / "certs" / "flow.crt"
CHAVE = RAIZ / "certs" / "flow.key"
CERT_NO_APP = RAIZ.parent / "SensorApp" / "app" / "src" / "main" / "res" / "raw" / "flow_cert.pem"
DIAS_PADRAO = 730


def gerar(cert: Path, chave: Path, nomes: list[str], ips: list[str], dias: int = DIAS_PADRAO) -> None:
    """Escreve o certificado autoassinado (RSA 2048, SHA-256) em `cert` e a chave privada em `chave` (0600 onde houver)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    par = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    publica = par.public_key()
    nome = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "flow")])
    agora = datetime.now(timezone.utc)
    alternativos = [x509.DNSName(n) for n in nomes] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    # CA:TRUE porque o mesmo arquivo e servidor e ancora de confianca (autoassinado); isso nao o torna uma autoridade
    # para ninguem: so quem o instalou como confianca (bridge, app, borda, cliente do banco) o aceita.
    certificado = (
        x509.CertificateBuilder()
        .subject_name(nome).issuer_name(nome).public_key(publica)
        .serial_number(x509.random_serial_number())
        .not_valid_before(agora - timedelta(minutes=5))          # folga para relogios um pouco atrasados
        .not_valid_after(agora + timedelta(days=dias))
        .add_extension(x509.SubjectAlternativeName(alternativos), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=True, key_cert_sign=True,
                                     content_commitment=False, data_encipherment=False, key_agreement=False,
                                     crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(publica), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(publica), critical=False)
        .sign(par, hashes.SHA256())
    )
    cert.parent.mkdir(parents=True, exist_ok=True)
    chave.parent.mkdir(parents=True, exist_ok=True)
    cert.write_bytes(certificado.public_bytes(serialization.Encoding.PEM))
    chave_pem = par.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption())
    fd = os.open(chave, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)    # 0600 desde a criacao (no Windows e ignorado)
    with os.fdopen(fd, "wb") as f:
        f.write(chave_pem)
    os.chmod(chave, 0o600)                                               # se o arquivo ja existia com outra permissao


def _ip_da_rede() -> str | None:
    """IP da interface que sai para a rede local (UDP nao envia nada: so escolhe a interface). None se nao houver rede."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


def _sem_repeticao(itens: list[str]) -> list[str]:
    return list(dict.fromkeys(itens))


def _copiar_para_o_app(cert: Path) -> bool:
    """Copia o certificado (publico) para o app, se a pasta do app existir. Devolve True se copiou algo."""
    if not (RAIZ.parent / "SensorApp" / "app").is_dir():
        return False
    CERT_NO_APP.parent.mkdir(parents=True, exist_ok=True)
    if CERT_NO_APP.is_file() and CERT_NO_APP.read_bytes() == cert.read_bytes():
        return False
    shutil.copyfile(cert, CERT_NO_APP)
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Gera o certificado TLS unico do Flow (autoassinado).")
    ap.add_argument("--ip", action="append", default=[], help="IP extra no certificado (repita para varios)")
    ap.add_argument("--dns", action="append", default=[], help="nome extra no certificado (repita para varios)")
    ap.add_argument("--dias", type=int, default=DIAS_PADRAO, help=f"validade em dias (padrao {DIAS_PADRAO})")
    ap.add_argument("--forcar", action="store_true", help="recria o par mesmo que ja exista")
    a = ap.parse_args(argv)

    if CERT.is_file() and CHAVE.is_file() and not a.forcar:
        print(f"certificado ja existe: {CERT.relative_to(RAIZ)} (nada a fazer; --forcar recria, p.ex. com outro --ip)")
        if _copiar_para_o_app(CERT):
            print(f"copiado para o app: {CERT_NO_APP.relative_to(RAIZ.parent)}")
        return 0

    ips = _sem_repeticao(["127.0.0.1", "::1", *([ip] if (ip := _ip_da_rede()) else []), *a.ip])
    nomes = _sem_repeticao(["localhost", *a.dns])
    try:
        for ip in ips:
            ipaddress.ip_address(ip)
        gerar(CERT, CHAVE, nomes, ips, a.dias)
    except ValueError as e:
        print(f"erro: {e}", file=sys.stderr)
        return 1
    print(f"certificado gerado: {CERT.relative_to(RAIZ)} (valido por {a.dias} dias)")
    print(f"chave privada:      {CHAVE.relative_to(RAIZ)} (segredo; fora do Git)")
    print(f"vale para:          {', '.join(nomes + ips)}")
    if _copiar_para_o_app(CERT):
        print(f"copiado para o app: {CERT_NO_APP.relative_to(RAIZ.parent)} (recompile o app)")
    if not any(ip not in ("127.0.0.1", "::1") for ip in ips):
        print("aviso: nenhum IP de rede no certificado; o celular nao conectara. Use --ip <IP do computador na rede>.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
