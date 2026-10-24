"""
Validacao ESTRUTURAL da borda: a unica porta de entrada dos dados dos dispositivos.

Usa os contratos de schemas/ (via comum.contratos). Confere forma, tipos e vocabulario; NAO decide nada de negocio
(faixa fisica, sentinelas, plausibilidade ficam nas regras, que na analise toleram campos e versoes desconhecidos).

O comportamento externo e o de sempre: corpo malformado -> 400 (CorpoInvalido); conteudo invalido de requisicao ja
autenticada -> o chamador descarta e responde 200. Nenhuma mensagem de erro daqui contem o conteudo recebido.
"""
from comum import const as const_comum
from comum import contratos


class CorpoInvalido(Exception):
    """Corpo da requisicao malformado. `str(e)` e a mensagem enviada ao cliente (400)."""


class EventoRetornoInvalido(Exception):
    pass


# ---------------------------------------------------------------- envelope e roteamento
def validar_envelope(envelope) -> None:
    """Corpo cru da requisicao: objeto com 'nonce' e 'ciphertext' (texto). Levanta CorpoInvalido."""
    if not isinstance(envelope, dict) or contratos.validar("envelope", envelope):
        raise CorpoInvalido("Corpo invalido: esperado {'nonce': ..., 'ciphertext': ...}")


def ler_user_id(valor) -> int | None:
    """user_id aceito: somente inteiro JSON (nao booleano). String (mesmo de digitos), decimal, null e ausente -> None.
    Evita que True vire 1, que 1.9 vire 1 e que "1" passe como 1 silenciosamente."""
    if isinstance(valor, int) and not isinstance(valor, bool):
        return valor
    return None


def validar_roteamento(corpo: dict) -> tuple[str, int]:
    """(source, user_id) de /dados e /eventos. 'source' ausente ou fora de const_comum.FONTES_VALIDAS e 'user_id' que nao e
    inteiro levantam CorpoInvalido (400), nessa ordem. Conferir contra o token e o proximo passo (authz)."""
    source = corpo.get("source")
    if not isinstance(source, str) or source not in const_comum.FONTES_VALIDAS:
        raise CorpoInvalido(f"Campo 'source' ausente ou invalido (esperado 'app' ou 'wokwi'): {source}")
    user_id = ler_user_id(corpo.get("user_id"))
    if user_id is None:
        raise CorpoInvalido(f"Campo 'user_id' ausente ou nao-inteiro: {corpo.get('user_id')}")
    return source, user_id


# ---------------------------------------------------------------- versao
def versao_atual_da_fonte(source: str) -> int:
    """Versao mais recente do contrato da fonte ('app' ou 'wokwi'), lida de const.py na hora da chamada."""
    if source == "app":
        return const_comum.SCHEMA_VERSION_APP
    if source == "wokwi":
        return const_comum.SCHEMA_VERSION_WOKWI
    raise ValueError(f"fonte desconhecida: {source!r}")


def verificar_versao(payload: dict, versao_atual: int) -> int | None:
    """Verifica `schemaVersion` (raiz do payload) ANTES de interpretar o resto. Nunca levanta excecao.

    `versao_atual` e a do contrato da rota (quem chama escolhe) e o piso vem de const.py:
      - ausente: vale const_comum.SCHEMA_VERSION_PADRAO;
      - de const_comum.SCHEMA_VERSION_MINIMA ate `versao_atual`: aceita (inclusive versao menor que a atual);
      - invalido: nao inteiro (inclui "3", 3.0, null), booleano, menor que a minima ou maior que `versao_atual`."""
    v = payload.get("schemaVersion", const_comum.SCHEMA_VERSION_PADRAO)
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    if v < const_comum.SCHEMA_VERSION_MINIMA or v > versao_atual:
        return None
    return v


# ---------------------------------------------------------------- telemetria
def erros_telemetria_wokwi(payload: dict) -> list[str]:
    """Estrutura de um evento de telemetria do Wokwi (contrato wokwi-telemetria). Vazia = valido."""
    return contratos.validar("wokwi-telemetria", payload)


def erros_telemetria_app(payload: dict) -> list[str]:
    """Estrutura da telemetria do app (contrato app-telemetria, tolerante). Vazia = valido."""
    return contratos.validar("app-telemetria", payload)


def erros_leitura_ambiente(bloco) -> list[str]:
    """Estrutura do bloco leitura_ambiente do app (contrato app-leitura-ambiente). Vazia = valido."""
    return contratos.validar("app-leitura-ambiente", bloco)


# ---------------------------------------------------------------- eventos de retorno
def validar_evento_retorno(payload: dict, source: str) -> dict:
    """Valida e normaliza um evento de POST /eventos. Devolve {eventType, state, messageId, device_id, source}.
    Levanta EventoRetornoInvalido.

    `source` deve ser uma das const_comum.FONTES_VALIDAS e JA conferida com o token pelo chamador (authz.exigir_escopo).
    Qualquer outro valor e erro de programacao (ValueError), nao conteudo invalido do cliente."""
    if source not in const_comum.FONTES_VALIDAS:
        raise ValueError(f"source deve ser uma de {sorted(const_comum.FONTES_VALIDAS)} (ja conferida com o token): {source!r}")
    erros = contratos.validar(f"{source}-eventos-retorno", payload)
    if erros:
        raise EventoRetornoInvalido("; ".join(erros[:3]))
    device_id = payload.get("device_id") or payload.get("deviceId")
    return {"eventType": payload["eventType"], "state": payload["state"], "messageId": payload["messageId"],
            "device_id": str(device_id), "source": source}
