"""
Criptografia do transporte (Marco 2).

Mecanismo escolhido: AES-256-GCM com chave simetrica pre-compartilhada.

Por que este mecanismo e nao outro (TLS, por exemplo):
- E o "menor mecanismo que atende ao requisito" (ver Aula 05): o app envia um
  lote pequeno a cada 10s, sem necessidade de sessao continua, handshake ou
  certificados. AES-GCM cifra e autentica cada lote de forma independente
  (uma chamada = uma mensagem cifrada), o que combina bem com um protocolo
  sem estado de conexao como o HTTP simples ja usado no projeto.
- GCM ja fornece integridade (tag de autenticacao), entao nao e necessario
  nenhum mecanismo adicional de assinatura para detectar adulteracao do
  payload em transito.

Limitacao assumida (documentada no README, secao "Risco principal"): a
chave e fixa e compartilhada entre app e servidor, sem rotacao ou troca de
chaves (key exchange). Isso e aceitavel apenas para esta fase de protótipo,
dentro da rede local. Em producao, a chave deveria ser negociada por
dispositivo (ex.: durante o pareamento) e nao ficar hardcoded no codigo.
"""

import os
import base64

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ---------------------------------------------------------------------------
# CHAVE COMPARTILHADA (32 bytes / AES-256), em base64.
# Precisa ser EXATAMENTE a mesma constante usada em
# SensorApp/.../CryptoUtils.kt (SHARED_KEY_B64).
#
# NOTA (nivel academico): esta chave e estatica e hardcoded de proposito,
# apenas para viabilizar a demonstracao do Marco 2 dentro do prazo da
# disciplina. Em um cenario de producao, ela NAO deveria ser fixa nem
# compartilhada por todos os dispositivos - o correto seria cada
# dispositivo (celular do idoso) gerar/receber sua propria chave durante
# um pareamento inicial com o servidor (ex.: troca de chaves via
# Diffie-Hellman/ECDH no primeiro cadastro, ou uma chave por dispositivo
# emitida e armazenada de forma segura, como Android Keystore), evitando
# que a mesma chave sirva para todos os idosos monitorados e permitindo
# revogar/trocar a chave de um dispositivo especifico sem afetar os demais.
# ---------------------------------------------------------------------------
SHARED_KEY_B64 = "nL+mtDjw5+64sqP0berkvBun36w9EFNxkyPjR+UHSik="

_key_bytes = base64.b64decode(SHARED_KEY_B64)
if len(_key_bytes) != 32:
    raise ValueError("SHARED_KEY_B64 precisa decodificar para 32 bytes (AES-256).")

_aesgcm = AESGCM(_key_bytes)

# Tamanho recomendado de nonce para GCM
NONCE_SIZE_BYTES = 12


class DecryptionError(Exception):
    """Falha ao decifrar ou autenticar o payload recebido."""


def decrypt_payload(nonce_b64: str, ciphertext_b64: str) -> str:
    """
    Decifra o payload recebido do app Android.

    Recebe nonce e ciphertext em base64 (formato enviado pelo app) e
    retorna o JSON original (em texto), ja validado pela tag GCM.
    """
    try:
        nonce = base64.b64decode(nonce_b64)
        ciphertext = base64.b64decode(ciphertext_b64)
        plaintext = _aesgcm.decrypt(nonce, ciphertext, None)
        return plaintext.decode("utf-8")
    except Exception as e:
        raise DecryptionError(f"Falha ao decifrar payload: {e}") from e


def encrypt_payload(plaintext: str) -> tuple[str, str]:
    """
    Cifra um texto (usado apenas em testes/simulacoes no servidor).
    Retorna (nonce_b64, ciphertext_b64).
    """
    nonce = os.urandom(NONCE_SIZE_BYTES)
    ciphertext = _aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce).decode(), base64.b64encode(ciphertext).decode()
