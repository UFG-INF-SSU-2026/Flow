#!/usr/bin/env python3
"""
Gera a CA PROPRIA do Flow, os certificados TLS de cada servico, assinados por ela, e a CA de DISPOSITIVOS (independente da
raiz), que so assina certificados de cliente. Roda de qualquer pasta; so precisa do pacote `cryptography` (ja esta em
requirements.txt).

    python scripts/gerar_certificado.py                      cria as CAs se faltarem e emite o que faltar; o que ja existe fica
    python scripts/gerar_certificado.py --ip 192.168.0.10    inclui o IP do computador na rede (o que o celular usa) na borda
    python scripts/gerar_certificado.py --forcar --ip ...    reemite os certificados de servico (a CA continua a mesma)
    python scripts/gerar_certificado.py --nome bridge        emite tambem certs/bridge.crt (API HTTPS do bridge e, com
                                                             clientAuth, a identidade dele na conexao com a borda)
    python scripts/gerar_certificado.py --forcar-ca          recria a CA e reemite tudo (o app precisa ser recompilado)

Arquivos (SensorServer/certs/, fora do Git):
    ca.crt                  certificado da CA (publico): a UNICA confianca de todos os clientes (borda -> analise,
                            borda -> banco, bridge, app). E o que o app embute e o que o bridge usa em FLOW_TLS_CA
    ca.key                  chave da CA (SEGREDO): so este script a le; nenhum servico a usa nem a monta
    ca-dispositivos.crt     certificado da CA de dispositivos (publico; autoassinada, NAO e emitida pela raiz): quem confere
                            certificados de cliente de dispositivos confia nesta e so nesta
    ca-dispositivos.key     chave da CA de dispositivos (SEGREDO): so a borda a le, para assinar o certificado de cada
                            dispositivo (POST /auth/certificado)
    borda.crt / borda.key   servidos pela borda (localhost, 127.0.0.1, ::1, o IP da rede detectado e os de --ip/--dns)
    analise.crt / .key      servidos pela analise (localhost, 127.0.0.1)
    banco.crt / banco.key   servidos pelo banco (localhost, 127.0.0.1); o docker compose monta so estes dois
    <nome>.crt / .key       um por --nome (mesmos nomes e IPs da borda)
    SensorApp/app/src/main/res/raw/flow_cert.pem   copia de ca.crt (publico) que o app embute como unica confianca
    sketch/ca_flow.h        cabecalho C++ com ca.crt (publico) em FLOW_CA_PEM, a CA em que o firmware confia (fora do Git)

Renovar um certificado de servico (--forcar) NAO troca a CA: app e bridge continuam confiando sem recompilar. So a troca da
CA (--forcar-ca) exige recompilar o app e o firmware e reemitir tambem os `--nome` que existirem. Um cliente que acesse o
servidor por um endereco fora da lista do certificado recusa a conexao: reemita com --forcar e o endereco novo.

A CA de dispositivos e criada quando faltar, em qualquer execucao, e nunca e recriada por --forcar nem por --forcar-ca (a raiz
nao e tocada por ela, nem ela pela raiz). Para recria-la, apague ca-dispositivos.crt e ca-dispositivos.key e rode o script de
novo. Sem rotacao automatica; quem se autentica no servidor e a conta, por token (e o certificado de cliente, conferido pela borda).

Depois de reemitir: `docker compose restart db` (o banco carrega o par ao subir) e reinicie borda e analise.
"""
import argparse
import ipaddress
import os
import re
import shutil
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent                   # SensorServer/
PASTA = RAIZ / "certs"
CA_CERT = PASTA / "ca.crt"
CA_CHAVE = PASTA / "ca.key"
CA_DISPOSITIVOS_CERT = PASTA / "ca-dispositivos.crt"
CA_DISPOSITIVOS_CHAVE = PASTA / "ca-dispositivos.key"
CERT_NO_APP = RAIZ.parent / "SensorApp" / "app" / "src" / "main" / "res" / "raw" / "flow_cert.pem"
CABECALHO_NO_SKETCH = RAIZ.parent / "sketch" / "ca_flow.h"
DIAS_PADRAO = 730                # certificados de servico
DIAS_CA = 3650                   # CAs (dez anos): renovar um servico nao as toca
NOME_CA = "flow-ca"
NOME_CA_DISPOSITIVOS = "flow-ca-dispositivos"
NOME_VALIDO = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SERVICOS_FIXOS = ("borda", "analise", "banco")
NOME_BRIDGE = "bridge"           # o unico certificado da raiz que tambem serve de cliente (o bridge na borda)


def _gravar_chave(chave: Path, par) -> None:
    from cryptography.hazmat.primitives import serialization
    chave.parent.mkdir(parents=True, exist_ok=True)
    pem = par.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    fd = os.open(chave, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)    # 0600 desde a criacao (no Windows e ignorado)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    os.chmod(chave, 0o600)                                               # se o arquivo ja existia com outra permissao


def _gravar_cert(cert: Path, certificado) -> None:
    from cryptography.hazmat.primitives import serialization
    cert.parent.mkdir(parents=True, exist_ok=True)
    cert.write_bytes(certificado.public_bytes(serialization.Encoding.PEM))


def criar_ca(cert: Path, chave: Path, dias: int = DIAS_CA, nome: str = NOME_CA) -> None:
    """Escreve uma CA do projeto (RSA 2048, SHA-256, autoassinada, CN `nome`) em `cert` e a chave privada em `chave` (0600
    onde houver)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    par = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    publica = par.public_key()
    assunto = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, nome)])
    agora = datetime.now(timezone.utc)
    certificado = (
        x509.CertificateBuilder()
        .subject_name(assunto).issuer_name(assunto).public_key(publica)
        .serial_number(x509.random_serial_number())
        .not_valid_before(agora - timedelta(minutes=5))          # folga para relogios um pouco atrasados
        .not_valid_after(agora + timedelta(days=dias))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)   # so assina certificados finais
        .add_extension(x509.KeyUsage(digital_signature=False, key_encipherment=False, key_cert_sign=True,
                                     content_commitment=False, data_encipherment=False, key_agreement=False,
                                     crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(publica), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(publica), critical=False)
        .sign(par, hashes.SHA256())
    )
    _gravar_cert(cert, certificado)
    _gravar_chave(chave, par)


def emitir(ca_cert: Path, ca_chave: Path, cert: Path, chave: Path, cn: str, nomes: list[str], ips: list[str],
           dias: int = DIAS_PADRAO, cliente: bool = False) -> None:
    """Emite um certificado de servico (RSA 2048, SHA-256) assinado pela CA, com os SAN `nomes` e `ips`. Com `cliente`, o
    certificado tambem serve para se autenticar como cliente (`clientAuth`), como o do bridge na borda.
    Escreve o certificado em `cert` e a chave privada em `chave` (0600 onde houver)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    ca = x509.load_pem_x509_certificate(ca_cert.read_bytes())
    ca_par = serialization.load_pem_private_key(ca_chave.read_bytes(), password=None)
    par = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    publica = par.public_key()
    agora = datetime.now(timezone.utc)
    fim = min(agora + timedelta(days=dias), ca.not_valid_after_utc)      # nunca vale mais que a CA
    alternativos = [x509.DNSName(n) for n in nomes] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    ski_da_ca = ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    certificado = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])).issuer_name(ca.subject)
        .public_key(publica)
        .serial_number(x509.random_serial_number())
        .not_valid_before(agora - timedelta(minutes=5))
        .not_valid_after(fim)
        .add_extension(x509.SubjectAlternativeName(alternativos), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=True, key_cert_sign=False,
                                     content_commitment=False, data_encipherment=False, key_agreement=False,
                                     crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]
                                             + ([ExtendedKeyUsageOID.CLIENT_AUTH] if cliente else [])), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(publica), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski_da_ca), critical=False)
        .sign(ca_par, hashes.SHA256())
    )
    _gravar_cert(cert, certificado)
    _gravar_chave(chave, par)


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


def _copiar_para_o_app(ca: Path) -> bool:
    """Copia o certificado da CA (publico) para o app, se a pasta do app existir. Devolve True se copiou algo."""
    if not (RAIZ.parent / "SensorApp" / "app").is_dir():
        return False
    CERT_NO_APP.parent.mkdir(parents=True, exist_ok=True)
    if CERT_NO_APP.is_file() and CERT_NO_APP.read_bytes() == ca.read_bytes():
        return False
    shutil.copyfile(ca, CERT_NO_APP)
    return True


def _cabecalho_do_sketch(ca: Path) -> str:
    """Conteudo de sketch/ca_flow.h: so o certificado PUBLICO da CA, na variavel FLOW_CA_PEM que o firmware usa."""
    pem = ca.read_text(encoding="ascii").strip()
    return ('#pragma once\n'
            '// Gerado por SensorServer/scripts/gerar_certificado.py: nao edite. Tem so o certificado PUBLICO da CA do projeto\n'
            '// (SensorServer/certs/ca.crt), o unico em que o firmware confia. Fica fora do Git; rode o script para recria-lo.\n'
            f'static const char FLOW_CA_PEM[] = R"PEM(\n{pem}\n)PEM";\n')


def _gerar_cabecalho_do_sketch(ca: Path) -> bool:
    """Escreve o cabecalho da CA para o sketch, se a pasta do sketch existir e o conteudo mudou. Devolve True se escreveu."""
    if not CABECALHO_NO_SKETCH.parent.is_dir():
        return False
    conteudo = _cabecalho_do_sketch(ca)
    if CABECALHO_NO_SKETCH.is_file() and CABECALHO_NO_SKETCH.read_text(encoding="utf-8") == conteudo:
        return False
    CABECALHO_NO_SKETCH.write_text(conteudo, encoding="utf-8", newline="\n")
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Gera a CA do Flow e os certificados TLS dos servicos, assinados por ela.")
    ap.add_argument("--ip", action="append", default=[], help="IP extra no certificado da borda (repita para varios)")
    ap.add_argument("--dns", action="append", default=[], help="nome extra no certificado da borda (repita para varios)")
    ap.add_argument("--nome", action="append", default=[],
                    help="emite tambem certs/<nome>.crt, p.ex. bridge (mesmos nomes e IPs da borda; repita para varios)")
    ap.add_argument("--dias", type=int, default=DIAS_PADRAO, help=f"validade dos certificados de servico (padrao {DIAS_PADRAO})")
    ap.add_argument("--forcar", action="store_true", help="reemite os certificados de servico (a CA continua a mesma)")
    ap.add_argument("--forcar-ca", action="store_true", help="recria a CA e reemite tudo (recompile o app)")
    a = ap.parse_args(argv)

    extras = _sem_repeticao(a.nome)
    for n in extras:
        if not NOME_VALIDO.match(n) or n in ("ca", *SERVICOS_FIXOS):
            print(f"erro: --nome invalido: {n!r} (use letras minusculas, digitos e hifen; nao repita ca, "
                  f"{', '.join(SERVICOS_FIXOS)})", file=sys.stderr)
            return 1

    ips_borda = _sem_repeticao(["127.0.0.1", "::1", *([ip] if (ip := _ip_da_rede()) else []), *a.ip])
    nomes_borda = _sem_repeticao(["localhost", *a.dns])
    # servico -> (nomes, ips). Analise e banco so respondem no proprio computador.
    pedidos = {"borda": (nomes_borda, ips_borda),
               "analise": (["localhost"], ["127.0.0.1"]),
               "banco": (["localhost"], ["127.0.0.1"]),
               **{n: (nomes_borda, ips_borda) for n in extras}}

    if CA_DISPOSITIVOS_CERT.is_file() != CA_DISPOSITIVOS_CHAVE.is_file():     # antes de criar qualquer coisa
        sobrou = CA_DISPOSITIVOS_CERT if CA_DISPOSITIVOS_CERT.is_file() else CA_DISPOSITIVOS_CHAVE
        print(f"erro: a CA de dispositivos esta incompleta (so existe {sobrou.relative_to(RAIZ)}). Apague-o e rode o script "
              f"de novo para recria-la.", file=sys.stderr)
        return 1

    try:
        for ip in ips_borda:
            ipaddress.ip_address(ip)

        recriar_ca = a.forcar_ca or not (CA_CERT.is_file() and CA_CHAVE.is_file())
        if recriar_ca and CA_CERT.is_file() != CA_CHAVE.is_file() and not a.forcar_ca:
            faltando = CA_CHAVE if CA_CERT.is_file() else CA_CERT
            print(f"erro: a CA esta incompleta (falta {faltando.relative_to(RAIZ)}). Para recomecar, use --forcar-ca.",
                  file=sys.stderr)
            return 1
        if recriar_ca:
            criar_ca(CA_CERT, CA_CHAVE)
            print(f"CA criada:          {CA_CERT.relative_to(RAIZ)} (valida por {DIAS_CA} dias)")
            print(f"chave da CA:        {CA_CHAVE.relative_to(RAIZ)} (segredo; so este script a le; fora do Git)")

        criou_dispositivos = not CA_DISPOSITIVOS_CERT.is_file()
        if criou_dispositivos:
            criar_ca(CA_DISPOSITIVOS_CERT, CA_DISPOSITIVOS_CHAVE, nome=NOME_CA_DISPOSITIVOS)
            print(f"CA de dispositivos: {CA_DISPOSITIVOS_CERT.relative_to(RAIZ)} (valida por {DIAS_CA} dias; independente da CA raiz)")
            print(f"chave dela:         {CA_DISPOSITIVOS_CHAVE.relative_to(RAIZ)} (segredo; so a borda a le; fora do Git)")

        emitidos = []
        for nome, (nomes, ips) in pedidos.items():
            cert, chave = PASTA / f"{nome}.crt", PASTA / f"{nome}.key"
            if cert.is_file() and chave.is_file() and not (a.forcar or recriar_ca):
                continue
            emitir(CA_CERT, CA_CHAVE, cert, chave, nome, nomes, ips, a.dias, cliente=nome == NOME_BRIDGE)
            emitidos.append(nome)
            print(f"certificado gerado: {cert.relative_to(RAIZ)} (valido por {a.dias} dias; "
                  f"vale para {', '.join(nomes + ips)})")
    except (ValueError, OSError) as e:
        print(f"erro: {e}", file=sys.stderr)
        return 1

    if not recriar_ca and not emitidos and not criou_dispositivos:
        print("CAs e certificados ja existem (nada a fazer; --forcar reemite os de servico, p.ex. com outro --ip)")
    if recriar_ca and a.forcar_ca:
        print("a CA mudou: recompile o app e o firmware e reemita os certificados extras (--nome) que existirem")
    if _copiar_para_o_app(CA_CERT):
        print(f"copiado para o app: {CERT_NO_APP.relative_to(RAIZ.parent)} ({'recompile o app' if recriar_ca else 'CA do projeto'})")
    if _gerar_cabecalho_do_sketch(CA_CERT):
        print(f"gerado para o sketch: {CABECALHO_NO_SKETCH.relative_to(RAIZ.parent)} "
              f"({'recompile o firmware' if recriar_ca else 'CA do projeto'})")
    if "borda" in emitidos and not any(ip not in ("127.0.0.1", "::1") for ip in ips_borda):
        print("aviso: nenhum IP de rede no certificado da borda; o celular nao conectara. "
              "Use --ip <IP do computador na rede>.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
