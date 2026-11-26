"""
v_comum.py - pecas compartilhadas pelos scripts de verificacao manual (teste_v2, v3, v5, v6) do projeto Flow.

Copie este arquivo JUNTO com os teste_v*.py para a pasta SensorServer/ (os scripts o importam). Ele usa modulos do proprio
projeto (borda.crypto_utils, comum.tls), por isso tudo roda DENTRO de SensorServer/. A verificacao TLS nunca e desligada:
a confianca e a CA do projeto (certs/ca.crt).

Variaveis opcionais: FLOW_BORDA_URL (padrao https://127.0.0.1:5000).
"""
import getpass
import json
import os
import sys
from pathlib import Path

import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from borda.crypto_utils import decrypt_payload, encrypt_payload
from comum import const as const_comum
from comum import tls

BASE = os.environ.get("FLOW_BORDA_URL", "https://127.0.0.1:5000").rstrip("/")
CA = tls.ca()


def env(obj: dict) -> dict:
    """Cifra o JSON no ENVELOPE (AES-256-GCM, chave compartilhada) que a borda exige em login, certificado e dados."""
    nonce, texto_cifrado = encrypt_payload(json.dumps(obj))
    return {"nonce": nonce, "ciphertext": texto_cifrado}


def decifrar(resposta: requests.Response) -> dict:
    """Inverso de env(): as respostas de sucesso da borda tambem vem cifradas."""
    envelope = resposta.json()
    return json.loads(decrypt_payload(envelope["nonce"], envelope["ciphertext"]))


def senha(variavel: str, pergunta: str) -> str:
    """Le a senha da variavel de ambiente; se nao existir, pergunta no terminal sem mostrar o que e digitado."""
    return os.environ.get(variavel) or getpass.getpass(pergunta)


def login(identificador: str, segredo: str, fonte: str | None, device_id: str | None) -> dict:
    """POST /auth/login como o app ou o firmware fazem. Devolve {access_token, user_ids, ...}."""
    r = requests.post(f"{BASE}/auth/login", verify=CA, timeout=15, json=env({
        "schemaVersion": const_comum.SCHEMA_VERSION_AUTH, "identificador": identificador, "segredo": segredo,
        "fonte": fonte, "device_id": device_id}))
    if r.status_code != 200:
        sys.exit(f"Login de {identificador} falhou: HTTP {r.status_code} {r.text.strip()}")
    return decifrar(r)


def pedir_certificado(token: str, device_id: str, pasta: Path, prefixo: str) -> tuple[str, str]:
    """Gera chave EC P-256 + CSR (CN = device_id); a borda assina e devolve o certificado de cliente. Chave e certificado
    ficam em arquivos temporarios na `pasta` (o requests so aceita certificado de cliente por arquivo)."""
    chave = ec.generate_private_key(ec.SECP256R1())
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, device_id)]))
           .sign(chave, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode("ascii"))
    r = requests.post(f"{BASE}/auth/certificado", json=env({"schemaVersion": 1, "csr": csr}),
                      headers={"Authorization": f"Bearer {token}"}, verify=CA, timeout=15)
    r.raise_for_status()
    cert, arq_chave = pasta / f"{prefixo}.crt", pasta / f"{prefixo}.key"
    cert.write_text(decifrar(r)["certificado"], encoding="ascii")
    arq_chave.touch(mode=0o600)
    arq_chave.write_bytes(chave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
    return str(cert), str(arq_chave)


def cuidador_le(rota: str, user_id: int, token: str, cert: tuple[str, str]) -> requests.Response:
    """GET /cuidador/<user_id>/<rota> como o app do cuidador (token + certificado de cliente). Devolve a resposta CRUA."""
    return requests.get(f"{BASE}/cuidador/{user_id}/{rota}", headers={"Authorization": f"Bearer {token}"},
                        verify=CA, cert=cert, timeout=15)
