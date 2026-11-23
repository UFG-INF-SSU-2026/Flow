"""
Autorizacao (Marco 3): papel -> permissoes, escopo por pessoa monitorada.
Negar por padrao: o que nao esta em POLITICA e proibido.
"""
import functools
from flask import current_app, g, jsonify, request

from borda import auth, emissao
from borda import const as const_borda

POLITICA: dict[str, set[str]] = {
    "idoso":       {"dados:enviar", "eventos:enviar", "push:receber", "certificado:emitir"},
    "dispositivo": {"dados:enviar", "eventos:enviar", "push:receber", "certificado:emitir"},
    "cuidador":    set(),                      # reservado (futuro)
    "admin":       {"dados:ler", "estado:ler"},
}
FONTE_DO_PAPEL = auth.FONTE_DO_PAPEL     # definida em auth.py (criar_conta tambem a usa)


class ForaDeEscopo(Exception):
    pass


def auditar(rota: str, status: int, codigo: str, conta: str | None = None) -> None:
    """Registra a negacao (401/403/413...) no log. Nunca o conteudo da requisicao. (Auditoria em banco: fora do escopo.)"""
    print(f"  [authz] negado {status} {codigo} rota={rota} conta={conta or '-'} ip={request.remote_addr}")


def negar(status: int, codigo: str, conta: str | None = None, mensagem: str | None = None):
    auditar(request.path, status, codigo, conta)
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else {}
    corpo = {"status": "erro", "codigo": codigo}
    if mensagem:
        corpo["mensagem"] = mensagem
    return jsonify(corpo), status, headers


def _exige_certificado_de_cliente(p: auth.Principal) -> bool:
    """O app (fonte que chega direto a borda) precisa apresentar o certificado emitido por /auth/certificado, quando a borda
    subiu com a CA de dispositivos (config EXIGIR_CERTIFICADO, ligada por borda/servico.py)."""
    return (bool(current_app.config.get("EXIGIR_CERTIFICADO")) and p.fonte == const_borda.FONTE_COM_CERTIFICADO_NA_BORDA
            and "certificado:emitir" in POLITICA.get(p.papel, set()))


def exige(permissao: str, fonte: str | None = None, certificado: bool = True):
    """Estagio 1: token valido + papel com a permissao + fonte exigida pela rota + (so o app) certificado de cliente que bate
    com o token. `certificado=False` e para a rota que EMITE o certificado: quem a chama ainda nao tem um."""
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
            if certificado and _exige_certificado_de_cliente(p):
                identidade = emissao.identidade_do_cliente(request.environ)
                if identidade is None:
                    return negar(401, "certificado_ausente", p.conta_id)         # o cliente emite o seu e tenta de novo
                if identidade != (p.conta_id, p.device_id):
                    return negar(403, "certificado_incompativel", p.conta_id)    # certificado de outra conta ou dispositivo
            g.principal = p
            g.token = cab[7:]
            return f(*args, **kwargs)
        return wrapper
    return deco


def exigir_dispositivo(principal: auth.Principal, payload: dict) -> None:
    """Estagio 2 (continuacao): o `device_id`/`deviceId` que o payload declarar deve ser o `device_id` do token
    (o informado no login). Sem isso, a conta de um usuario poderia enviar eventos em nome do device de outro e
    fazer os eventos reais dele serem descartados como duplicados. Campo ausente ou vazio nao declara nada."""
    for campo in ("device_id", "deviceId"):
        valor = payload.get(campo)
        if valor is None or valor == "":
            continue
        if valor != principal.device_id:
            raise ForaDeEscopo(f"{campo} do payload diferente do device_id do token")


def exigir_escopo(principal: auth.Principal, user_id: int, source: str) -> None:
    """Estagio 2 (apos decifrar): user_id do payload deve estar nas claims; source deve ser a do token.

    `source` e obrigatorio (sem valor padrao): nao existe caminho que dispense a checagem. O chamador
    deve rejeitar com 400 a requisicao sem `source` ANTES de chamar esta funcao."""
    if user_id not in principal.user_ids:
        raise ForaDeEscopo(f"user_id {user_id} fora do escopo da conta")
    if source != principal.fonte:
        raise ForaDeEscopo(f"source '{source}' diferente da fonte do token ('{principal.fonte}')")
