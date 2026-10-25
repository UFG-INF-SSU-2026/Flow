"""
Autenticacao (Marco 3): contas, senhas, login, refresh e tokens.

Nao conhece Flask. A camada HTTP fica em borda/server.py; a checagem por rota em borda/authz.py.

Contas e sessoes (hash do refresh token) moram no banco, pelo repositorio (flow_db). As funcoes que o usam recebem `repo` por
parametro opcional; sem ele, valem o repositorio instalado pela borda (ou o banco falso, nos testes). So login e refresh
dependem do banco: o access token e um JWT e e conferido sem ele.

Uso (CLI, exige o banco no ar e o .env preenchido):
    python -m borda.auth criar-conta --id idoso1 --papel idoso --user-id 1 --fontes app
    python -m borda.auth criar-conta --id bridge1 --papel dispositivo --user-id 1 --fontes wokwi --device-id esp32-1
"""
import argparse
import getpass
import hashlib
import hmac
import os
import secrets
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import jwt  # PyJWT

from borda import const as const_borda   # TTLs, bloqueio, caminhos, papeis e custo do scrypt
from comum import const as const_comum   # fontes validas
from flow_db import repositorios

_lock = threading.RLock()
_falhas: dict[str, list[float]] = {}
_bloqueado_ate: dict[str, float] = {}


# Fonte que cada papel pode usar (authz.py reexporta). Mora aqui porque criar_conta valida e authz importa auth.
FONTE_DO_PAPEL = {"idoso": "app", "dispositivo": "wokwi"}


class AuthError(Exception):
    codigo = "nao_autenticado"
    # So login() preenche: True quando o identificador e de uma conta que existe. A auditoria usa isto para
    # gravar `conta` apenas de contas reais; senao o texto digitado pelo cliente iria para o log.
    conta_existe: bool = False


class CredenciaisInvalidas(AuthError):
    codigo = "credenciais_invalidas"


class Bloqueado(AuthError):
    codigo = "bloqueado"


class DeviceIdObrigatorio(AuthError):
    """Login de `idoso` ou `dispositivo` sem device_id. A borda responde 400; nao conta como falha de senha."""
    codigo = "device_id_obrigatorio"


class DeviceNaoPermitido(AuthError):
    """Login de `dispositivo` com um device_id que nao esta na lista da conta. A borda responde 403; nao conta como falha de senha."""
    codigo = "device_nao_permitido"


class TokenInvalido(AuthError):
    codigo = "token_invalido"


class ServicoIndisponivel(AuthError):
    """O banco falhou durante login ou refresh. A borda responde 503 (nao e credencial errada: nao conta como falha)."""
    codigo = "servico_indisponivel"


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
            "FLOW_JWT_SECRET ausente ou com menos de 32 bytes. Defina em SensorServer/.env "
            "(modelo: .env.example) ou no ambiente. Gere um com: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    return s


def exigir_segredo() -> None:
    """Chamado na subida do servidor: sem segredo, o servidor nao sobe."""
    _segredo()


# ------------------------------- senhas ------------------------------------
def hash_senha(senha: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(senha.encode(), salt=salt,
                       n=const_borda.SCRYPT_N, r=const_borda.SCRYPT_R, p=const_borda.SCRYPT_P, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def _verificar_senha(senha: str, armazenado: str) -> bool:
    try:
        _, salt_hex, h_hex = armazenado.split("$")
        h = hashlib.scrypt(senha.encode(), salt=bytes.fromhex(salt_hex),
                           n=const_borda.SCRYPT_N, r=const_borda.SCRYPT_R, p=const_borda.SCRYPT_P, dklen=32)
        return hmac.compare_digest(h.hex(), h_hex)
    except Exception:
        return False


_HASH_FALSO = hash_senha("hash-falso-para-igualar-tempo")  # conta inexistente gasta o mesmo tempo


# ------------------------------ persistencia -------------------------------
def _repo(repo=None):
    r = repo if repo is not None else repositorios.repo()
    if r is None:
        raise ServicoIndisponivel("banco nao iniciado")
    return r


def _instante(epoch_s: float) -> datetime:
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc)


def _no_banco(operacao, *args):
    """Falha do banco em login/refresh vira ServicoIndisponivel (503); a causa fica encadeada, nunca vai ao cliente."""
    try:
        return operacao(*args)
    except Exception as e:  # noqa: BLE001  (erro de conexao, timeout, pool esgotado...)
        raise ServicoIndisponivel(f"{type(e).__name__}") from e


def criar_conta(conta_id: str, papel: str, user_ids: list[int], fontes: list[str], senha: str, repo=None,
                device_ids: list[str] | None = None) -> None:
    if papel not in const_borda.PAPEIS_VALIDOS:
        raise ValueError(f"papel invalido: {papel}")
    if not set(fontes) <= const_comum.FONTES_VALIDAS:
        raise ValueError(f"fontes invalidas: {fontes}")
    for uid in user_ids:
        if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
            raise ValueError(f"user_id invalido (esperado inteiro nao negativo): {uid!r}")
    if papel in ("idoso", "dispositivo"):
        if len(user_ids) != 1:
            raise ValueError("papeis 'idoso' e 'dispositivo' exigem exatamente um --user-id")
        permitida = FONTE_DO_PAPEL[papel]
        if not fontes or set(fontes) != {permitida}:
            raise ValueError(f"papel '{papel}' exige fontes == ['{permitida}'], recebeu {fontes}")
    if papel == "admin" and user_ids:
        raise ValueError("papel 'admin' nao tem --user-id (ele nao e escopado a uma pessoa)")
    device_ids = list(device_ids or [])
    if papel == "dispositivo" and not device_ids:
        raise ValueError("papel 'dispositivo' exige ao menos um --device-id (os que o login dela pode declarar)")
    if papel != "dispositivo" and device_ids:
        raise ValueError("--device-id so vale para o papel 'dispositivo' (o device_id do idoso e livre)")
    for d in device_ids:
        if not isinstance(d, str) or not d or not device_id_valido(d):
            raise ValueError(f"device_id invalido (texto de 1 a {const_comum.MAX_DEVICE_ID_LEN} caracteres): {d!r}")
    criada = _repo(repo).criar_conta({"id": conta_id, "papel": papel, "user_ids": list(user_ids), "fontes": list(fontes),
                                      "device_ids": device_ids, "hash": hash_senha(senha), "criada_em": _instante(time.time())})
    if not criada:
        raise ValueError(f"conta ja existe: {conta_id}")


# --------------------------------- tokens ----------------------------------
def _emitir_access(conta: dict, fonte: str | None, device_id: str | None, agora: float) -> str:
    claims = {
        "sub": conta["id"], "papel": conta["papel"], "user_ids": conta["user_ids"],
        "fonte": fonte, "device_id": device_id,
        "iat": int(agora), "exp": int(agora) + const_borda.ACCESS_TTL_S, "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, _segredo(), algorithm="HS256")


def verificar_token(token: str) -> Principal:
    try:
        c = jwt.decode(token, _segredo(), algorithms=["HS256"], options={"require": ["exp", "sub"]})
    except jwt.PyJWTError as e:
        raise TokenInvalido(str(e)) from e
    return Principal(c["sub"], c["papel"], tuple(c.get("user_ids", [])), c.get("fonte"), c.get("device_id"),
                     float(c["exp"]))


def _par_de_tokens(conta: dict, fonte, device_id, agora: float, repo) -> dict:
    refresh_token = secrets.token_urlsafe(48)
    repo.podar_sessoes(_instante(agora))   # limpa as vencidas
    repo.gravar_sessao({"token_hash": hashlib.sha256(refresh_token.encode()).hexdigest(), "conta_id": conta["id"],
                        "fonte": fonte, "device_id": device_id, "expira_em": _instante(agora + const_borda.REFRESH_TTL_S)})
    return {
        "access_token": _emitir_access(conta, fonte, device_id, agora),
        "refresh_token": refresh_token,
        "expires_in": const_borda.ACCESS_TTL_S,
        "papel": conta["papel"],
        "user_ids": conta["user_ids"],
    }


# ---------------------------------- login ----------------------------------
def _podar_login(agora: float) -> None:
    """Mantem _falhas e _bloqueado_ate pequenos: os identificadores vem do cliente (inclusive os que nao existem).
    Sai o que ja venceu (bloqueio passado; falhas todas fora da janela). Se ainda passar de
    const_borda.MAX_IDENTIFICADORES_LOGIN, saem primeiro os de falha mais antiga e depois os de bloqueio que vence antes."""
    for ident in [i for i, ate in _bloqueado_ate.items() if ate <= agora]:
        del _bloqueado_ate[ident]
    for ident in [i for i, fs in _falhas.items() if not fs or agora - fs[-1] >= const_borda.JANELA_FALHAS_S]:
        del _falhas[ident]
    for tabela, chave in ((_falhas, lambda i: _falhas[i][-1]), (_bloqueado_ate, _bloqueado_ate.get)):
        excesso = len(tabela) - const_borda.MAX_IDENTIFICADORES_LOGIN
        if excesso > 0:
            for ident in sorted(tabela, key=chave)[:excesso]:
                del tabela[ident]


def _checar_bloqueio(ident: str, agora: float) -> None:
    if _bloqueado_ate.get(ident, 0) > agora:
        raise Bloqueado()


def _registrar_falha(ident: str, agora: float) -> None:
    fs = [t for t in _falhas.get(ident, []) if agora - t < const_borda.JANELA_FALHAS_S] + [agora]
    _falhas[ident] = fs
    if len(fs) >= const_borda.MAX_FALHAS:
        _bloqueado_ate[ident] = agora + const_borda.BLOQUEIO_S
        del _falhas[ident]   # o bloqueio recomeca a contagem; nao deixa entrada vazia
    _podar_login(agora)      # depois de registrar: a entrada nova e a mais recente e nunca e a que sai


def device_id_valido(device_id) -> bool:
    """None (sem dispositivo) ou string de ate const_comum.MAX_DEVICE_ID_LEN caracteres. device_id vira chave do hub SSE."""
    return device_id is None or (isinstance(device_id, str) and len(device_id) <= const_comum.MAX_DEVICE_ID_LEN)


def login(identificador: str, segredo: str, fonte: str | None, device_id: str | None, repo=None) -> dict:
    if not device_id_valido(device_id):   # defesa; server.login ja responde 400 antes de chegar aqui
        raise ValueError("device_id invalido")
    agora = time.time()
    r = _repo(repo)
    with _lock:
        conta = _no_banco(r.buscar_conta, identificador)   # banco fora: 503, sem contar como falha de senha
        existe = conta is not None
        try:
            _checar_bloqueio(identificador, agora)
        except Bloqueado as e:
            e.conta_existe = existe
            raise
        senha_ok = _verificar_senha(segredo, conta["hash"] if conta else _HASH_FALSO)
        fonte_ok = existe and (fonte in conta["fontes"] or (conta["papel"] == "admin" and fonte is None))
        if not (existe and senha_ok and fonte_ok):
            _registrar_falha(identificador, agora)
            erro = CredenciaisInvalidas()
            erro.conta_existe = existe
            raise erro
        _falhas.pop(identificador, None)
        # So depois de a credencial estar certa (quem erra a senha nao descobre o que a conta exige). Nenhum dos dois
        # conta como falha de senha.
        if conta["papel"] in ("idoso", "dispositivo"):
            if not device_id:
                erro = DeviceIdObrigatorio()
                erro.conta_existe = True
                raise erro
            if conta["papel"] == "dispositivo" and device_id not in (conta.get("device_ids") or []):
                erro = DeviceNaoPermitido()
                erro.conta_existe = True
                raise erro
        return _no_banco(_par_de_tokens, conta, fonte, device_id, agora, r)


def refresh(refresh_token: str, repo=None) -> dict:
    agora = time.time()
    h = hashlib.sha256(refresh_token.encode()).hexdigest()
    r = _repo(repo)
    motivo = "refresh invalido ou expirado"
    with _lock:
        try:
            with r.transacao() as tx:   # uma transacao: se o banco falhar no meio, a sessao NAO e consumida
                s = tx.consumir_sessao(h)   # uso unico: se existia, sai do banco ja nesta chamada, valida ou vencida
                if s is not None and s["expira_em"] > _instante(agora):
                    conta = tx.buscar_conta(s["conta_id"])
                    if conta is not None:
                        return _par_de_tokens(conta, s["fonte"], s["device_id"], agora, tx)
                    motivo = "conta removida"
        except Exception as e:  # noqa: BLE001
            raise ServicoIndisponivel(f"{type(e).__name__}") from e
    raise TokenInvalido(motivo)


# ----------------------------------- CLI -----------------------------------
def _cli() -> int:
    ap = argparse.ArgumentParser(description="Gestao de contas do Flow")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("criar-conta")
    c.add_argument("--id", required=True)
    c.add_argument("--papel", required=True, choices=sorted(const_borda.PAPEIS_VALIDOS))
    c.add_argument("--user-id", type=int, nargs="*", default=[])
    c.add_argument("--fontes", nargs="*", default=[])
    c.add_argument("--device-id", action="append", default=[],
                   help="device_id permitido no login (so papel dispositivo, onde e obrigatorio; repita para varios)")
    a = ap.parse_args()
    senha = os.environ.get("FLOW_SENHA") or getpass.getpass("Senha: ")
    try:
        repositorios.iniciar()   # abre o banco pelo .env (FLOW_DB_*); sem banco no ar, recusa com mensagem clara
    except RuntimeError as e:
        print(f"erro: {e}", file=sys.stderr)
        return 1
    try:
        criar_conta(a.id, a.papel, a.user_id, a.fontes, senha, device_ids=a.device_id)
    except ValueError as e:
        print(f"erro: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001  (o banco caiu no meio: mensagem curta, sem detalhes de conexao)
        print(f"erro: o banco falhou ao gravar a conta ({type(e).__name__}).", file=sys.stderr)
        return 1
    finally:
        repositorios.desinstalar()
    print(f"conta '{a.id}' criada ({a.papel}, user_ids={a.user_id}, fontes={a.fontes}"
          + (f", device_ids={a.device_id}" if a.device_id else "") + ")")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
