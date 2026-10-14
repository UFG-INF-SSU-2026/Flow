"""
Autorizacao (Marco 3): papel -> permissoes, escopo por pessoa monitorada, auditoria.
Negar por padrao: o que nao esta em POLITICA e proibido.
"""
import functools
import json
import os
from datetime import datetime, timezone

from flask import g, jsonify, request

import auth
import const   # DATA_DIR (e AUDITORIA_MAX_BYTES)

POLITICA: dict[str, set[str]] = {
    "idoso":       {"dados:enviar", "eventos:enviar", "push:receber"},
    "dispositivo": {"dados:enviar", "eventos:enviar", "push:receber"},
    "cuidador":    set(),                      # reservado (futuro)
    "admin":       {"dados:ler", "estado:ler"},
}
FONTE_DO_PAPEL = {"idoso": "app", "dispositivo": "wokwi"}


class ForaDeEscopo(Exception):
    pass


def auditar(rota: str, status: int, codigo: str, conta: str | None = None) -> None:
    """Registra negacoes (401/403). Nunca grava o conteudo da requisicao."""
    os.makedirs(const.DATA_DIR, exist_ok=True)
    linha = {"timestamp": datetime.now(timezone.utc).isoformat(), "rota": rota,
             "status": status, "motivo": codigo, "conta": conta, "ip": request.remote_addr}
    with open(os.path.join(const.DATA_DIR, "auditoria.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(linha, ensure_ascii=False) + "\n")


def negar(status: int, codigo: str, conta: str | None = None):
    auditar(request.path, status, codigo, conta)
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else {}
    return jsonify({"status": "erro", "codigo": codigo}), status, headers


def exige(permissao: str, fonte: str | None = None):
    """Estagio 1: token valido + papel com a permissao + fonte exigida pela rota."""
    def deco(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            cab = request.headers.get("Authorization", "")
            if not cab.startswith("Bearer "):
                return negar(401, "token_ausente")
            try:
                p = auth.verificar_token(cab[7:])
            except auth.AuthError as e:
                return negar(401, e.codigo)
            if permissao not in POLITICA.get(p.papel, set()):
                return negar(403, "sem_permissao", p.conta_id)
            if p.papel in FONTE_DO_PAPEL and p.fonte != FONTE_DO_PAPEL[p.papel]:
                return negar(403, "fonte_invalida", p.conta_id)   # idoso so como 'app', dispositivo so como 'wokwi'
            if fonte is not None and p.fonte != fonte:
                return negar(403, "fonte_invalida", p.conta_id)   # rota exige uma fonte especifica
            g.principal = p
            g.token = cab[7:]
            return f(*args, **kwargs)
        return wrapper
    return deco


def exigir_escopo(principal: auth.Principal, user_id: int, source: str) -> None:
    """Estagio 2 (apos decifrar): user_id do payload deve estar nas claims; source deve ser a do token.

    `source` e obrigatorio (sem valor padrao): nao existe caminho que dispense a checagem. O chamador
    deve rejeitar com 400 a requisicao sem `source` ANTES de chamar esta funcao."""
    if user_id not in principal.user_ids:
        raise ForaDeEscopo(f"user_id {user_id} fora do escopo da conta")
    if source != principal.fonte:
        raise ForaDeEscopo(f"source '{source}' diferente da fonte do token ('{principal.fonte}')")
