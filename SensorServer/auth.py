"""
Autenticacao (Marco 3): contas, senhas, login, refresh e tokens.

Nao conhece Flask. A camada HTTP fica em server.py; a checagem por rota em authz.py.

Uso (CLI):
    python auth.py criar-conta --id idoso1 --papel idoso --user-id 1 --fontes app
"""
import argparse
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
import uuid
from dataclasses import dataclass

import jwt  # PyJWT

import const   # TTLs, bloqueio, caminhos, papeis, fontes e custo do scrypt (ver const.py)

_lock = threading.RLock()
_falhas: dict[str, list[float]] = {}
_bloqueado_ate: dict[str, float] = {}


class AuthError(Exception):
    codigo = "nao_autenticado"


class CredenciaisInvalidas(AuthError):
    codigo = "credenciais_invalidas"


class Bloqueado(AuthError):
    codigo = "bloqueado"


class TokenInvalido(AuthError):
    codigo = "token_invalido"


@dataclass(frozen=True)
class Principal:
    conta_id: str
    papel: str
    user_ids: tuple
    fonte: str | None
    device_id: str | None
    exp: float          # exp do access token (epoch s); o hub SSE usa para encerrar o stream (sem 2o decode)


# ------------------------------- segredo -----------------------------------
def _segredo() -> str:
    s = os.environ.get("FLOW_JWT_SECRET", "")
    if len(s.encode()) < 32:
        raise RuntimeError(
            "FLOW_JWT_SECRET ausente ou com menos de 32 bytes. "
            "Gere um com: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    return s


def exigir_segredo() -> None:
    """Chamado na subida do servidor: sem segredo, o servidor nao sobe."""
    _segredo()


# ------------------------------- senhas ------------------------------------
def hash_senha(senha: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(senha.encode(), salt=salt,
                       n=const.SCRYPT_N, r=const.SCRYPT_R, p=const.SCRYPT_P, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def _verificar_senha(senha: str, armazenado: str) -> bool:
    try:
        _, salt_hex, h_hex = armazenado.split("$")
        h = hashlib.scrypt(senha.encode(), salt=bytes.fromhex(salt_hex),
                           n=const.SCRYPT_N, r=const.SCRYPT_R, p=const.SCRYPT_P, dklen=32)
        return hmac.compare_digest(h.hex(), h_hex)
    except Exception:
        return False


_HASH_FALSO = hash_senha("hash-falso-para-igualar-tempo")  # conta inexistente gasta o mesmo tempo


# ------------------------------ persistencia -------------------------------
def _ler_json(path: str, padrao):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return padrao


def _gravar_json(path: str, dados) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def criar_conta(conta_id: str, papel: str, user_ids: list[int], fontes: list[str], senha: str) -> None:
    if papel not in const.PAPEIS_VALIDOS:
        raise ValueError(f"papel invalido: {papel}")
    if not set(fontes) <= const.FONTES_VALIDAS:
        raise ValueError(f"fontes invalidas: {fontes}")
    if papel in ("idoso", "dispositivo") and len(user_ids) != 1:
        raise ValueError("papeis 'idoso' e 'dispositivo' exigem exatamente um --user-id")
    with _lock:
        contas = _ler_json(const.CONTAS_PATH, {})
        if conta_id in contas:
            raise ValueError(f"conta ja existe: {conta_id}")
        contas[conta_id] = {"id": conta_id, "papel": papel, "user_ids": user_ids,
                            "fontes": fontes, "hash": hash_senha(senha)}
        _gravar_json(const.CONTAS_PATH, contas)


# --------------------------------- tokens ----------------------------------
def _emitir_access(conta: dict, fonte: str | None, device_id: str | None, agora: float) -> str:
    claims = {
        "sub": conta["id"], "papel": conta["papel"], "user_ids": conta["user_ids"],
        "fonte": fonte, "device_id": device_id,
        "iat": int(agora), "exp": int(agora) + const.ACCESS_TTL_S, "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, _segredo(), algorithm="HS256")


def verificar_token(token: str) -> Principal:
    try:
        c = jwt.decode(token, _segredo(), algorithms=["HS256"], options={"require": ["exp", "sub"]})
    except jwt.PyJWTError as e:
        raise TokenInvalido(str(e)) from e
    return Principal(c["sub"], c["papel"], tuple(c.get("user_ids", [])), c.get("fonte"), c.get("device_id"),
                     float(c["exp"]))


def _par_de_tokens(conta: dict, fonte, device_id, agora: float) -> dict:
    refresh_token = secrets.token_urlsafe(48)
    sessoes = _ler_json(const.SESSOES_PATH, {})
    sessoes = {h: s for h, s in sessoes.items() if s["exp"] > agora}  # limpa vencidas
    sessoes[hashlib.sha256(refresh_token.encode()).hexdigest()] = {
        "conta_id": conta["id"], "fonte": fonte, "device_id": device_id, "exp": agora + const.REFRESH_TTL_S,
    }
    _gravar_json(const.SESSOES_PATH, sessoes)
    return {
        "access_token": _emitir_access(conta, fonte, device_id, agora),
        "refresh_token": refresh_token,
        "expires_in": const.ACCESS_TTL_S,
        "papel": conta["papel"],
        "user_ids": conta["user_ids"],
    }


# ---------------------------------- login ----------------------------------
def _checar_bloqueio(ident: str, agora: float) -> None:
    if _bloqueado_ate.get(ident, 0) > agora:
        raise Bloqueado()


def _registrar_falha(ident: str, agora: float) -> None:
    fs = [t for t in _falhas.get(ident, []) if agora - t < const.JANELA_FALHAS_S] + [agora]
    _falhas[ident] = fs
    if len(fs) >= const.MAX_FALHAS:
        _bloqueado_ate[ident] = agora + const.BLOQUEIO_S
        _falhas[ident] = []


def device_id_valido(device_id) -> bool:
    """None (sem dispositivo) ou string de ate const.MAX_DEVICE_ID_LEN caracteres. device_id vira chave do hub SSE (R-09)."""
    return device_id is None or (isinstance(device_id, str) and len(device_id) <= const.MAX_DEVICE_ID_LEN)


def login(identificador: str, segredo: str, fonte: str | None, device_id: str | None) -> dict:
    if not device_id_valido(device_id):   # defesa; server.login ja responde 400 antes de chegar aqui
        raise ValueError("device_id invalido")
    agora = time.time()
    with _lock:
        _checar_bloqueio(identificador, agora)
        conta = _ler_json(const.CONTAS_PATH, {}).get(identificador)
        senha_ok = _verificar_senha(segredo, conta["hash"] if conta else _HASH_FALSO)
        fonte_ok = bool(conta) and (fonte in conta["fontes"] or (conta["papel"] == "admin" and fonte is None))
        if not (conta and senha_ok and fonte_ok):
            _registrar_falha(identificador, agora)
            raise CredenciaisInvalidas()
        _falhas.pop(identificador, None)
        return _par_de_tokens(conta, fonte, device_id, agora)


def refresh(refresh_token: str) -> dict:
    agora = time.time()
    h = hashlib.sha256(refresh_token.encode()).hexdigest()
    with _lock:
        sessoes = _ler_json(const.SESSOES_PATH, {})
        s = sessoes.pop(h, None)  # uso unico: some sempre, valido ou nao
        if s is None or s["exp"] <= agora:
            raise TokenInvalido("refresh invalido ou expirado")
        _gravar_json(const.SESSOES_PATH, sessoes)
        conta = _ler_json(const.CONTAS_PATH, {}).get(s["conta_id"])
        if conta is None:
            raise TokenInvalido("conta removida")
        return _par_de_tokens(conta, s["fonte"], s["device_id"], agora)


# ----------------------------------- CLI -----------------------------------
def _cli() -> int:
    ap = argparse.ArgumentParser(description="Gestao de contas do Flow")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("criar-conta")
    c.add_argument("--id", required=True)
    c.add_argument("--papel", required=True, choices=sorted(const.PAPEIS_VALIDOS))
    c.add_argument("--user-id", type=int, nargs="*", default=[])
    c.add_argument("--fontes", nargs="*", default=[])
    a = ap.parse_args()
    senha = os.environ.get("FLOW_SENHA") or getpass.getpass("Senha: ")
    try:
        criar_conta(a.id, a.papel, a.user_id, a.fontes, senha)
    except ValueError as e:
        print(f"erro: {e}", file=sys.stderr)
        return 1
    print(f"conta '{a.id}' criada ({a.papel}, user_ids={a.user_id}, fontes={a.fontes})")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
