"""Conveniencias sobre os contratos: validar por nome, vocabulario (enums) e versao documentada no schema."""
from . import schemas


def validar(contrato: str, valor) -> list[str]:
    """Erros estruturais de `valor` contra o contrato `contrato` (lista vazia = valido)."""
    return schemas.validar(schemas.carregar(contrato), valor)


def obrigatorios(contrato: str) -> list[str]:
    """Campos obrigatorios da raiz do contrato, na ordem em que o schema os lista."""
    return list(schemas.carregar(contrato).get("required", []))


def estados_por_evento(contrato: str, campo_tipo: str = "eventType", campo_estado: str = "state") -> dict[str, set[str]]:
    """Vocabulario `state` aceito por cada `eventType`, lido dos blocos allOf/if/then do schema."""
    saida: dict[str, set[str]] = {}
    for bloco in schemas.carregar(contrato).get("allOf", []):
        tipo = bloco.get("if", {}).get("properties", {}).get(campo_tipo, {}).get("const")
        estados = bloco.get("then", {}).get("properties", {}).get(campo_estado, {}).get("enum")
        if tipo is not None and estados is not None:
            saida[tipo] = set(estados)
    return saida


def enum(contrato: str, campo: str) -> list:
    """Valores permitidos de um campo da raiz do contrato (propriedade com `enum`)."""
    return list(schemas.carregar(contrato)["properties"][campo]["enum"])


def versao_documentada(contrato: str) -> int | None:
    """Versao atual do contrato escrita no proprio schema (`x-versao`); None se o contrato nao e versionado."""
    return schemas.carregar(contrato).get("x-versao")
