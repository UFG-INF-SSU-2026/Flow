"""
Carregador dos contratos (SensorServer/schemas/*.json) e validador de um SUBCONJUNTO de JSON Schema.

Por que um validador proprio: o projeto nao tem outra dependencia para isso, as regras estruturais sao poucas e o
comportamento (tipos estritos, sem conteudo do valor nas mensagens de erro) precisa ser exatamente o do servidor.

Palavras-chave suportadas: type, enum, const, required, properties, items, minimum, maximum, minLength, maxLength,
minItems, pattern, allOf, anyOf, if/then. Palavras de anotacao ($schema, $id, title, description, $comment, examples,
default e qualquer "x-...") sao ignoradas. Qualquer outra palavra levanta SchemaInvalido ao carregar (erro de digitacao
num schema nao passa em silencio).

Regras de tipo (iguais as do servidor): "integer" so aceita int (nao bool, nao float, mesmo 3.0); "number" aceita int
ou float finito (nao bool); "boolean" so bool. Propriedades desconhecidas sao SEMPRE aceitas (tolerancia a evolucao).

As mensagens de erro trazem so o caminho e a regra violada, NUNCA o valor recebido (o conteudo da requisicao nao vai
para log nem para resposta).
"""
import json
import math
import re
from pathlib import Path

PASTA = Path(__file__).resolve().parent.parent / "schemas"

_ANOTACOES = {"$schema", "$id", "title", "description", "$comment", "examples", "default"}
_VALIDACAO = {"type", "enum", "const", "required", "properties", "items", "minimum", "maximum", "minLength",
              "maxLength", "minItems", "pattern", "allOf", "anyOf", "if", "then"}
_TIPOS = {"object", "array", "string", "integer", "number", "boolean", "null"}


class SchemaInvalido(ValueError):
    """O arquivo de schema usa palavra-chave ou tipo fora do subconjunto suportado."""


class ContratoDesconhecido(KeyError):
    pass


_cache: dict[str, dict] = {}


def _conferir(schema, origem: str) -> None:
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        raise SchemaInvalido(f"{origem}: schema deve ser objeto")
    for chave, valor in schema.items():
        if chave in _ANOTACOES or chave.startswith("x-"):
            continue
        if chave not in _VALIDACAO:
            raise SchemaInvalido(f"{origem}: palavra-chave nao suportada: {chave}")
        if chave == "type":
            for t in ([valor] if isinstance(valor, str) else valor):
                if t not in _TIPOS:
                    raise SchemaInvalido(f"{origem}: tipo desconhecido: {t}")
        elif chave == "properties":
            for nome, sub in valor.items():
                _conferir(sub, f"{origem}.{nome}")
        elif chave == "items":
            _conferir(valor, f"{origem}[]")
        elif chave in ("allOf", "anyOf"):
            for i, sub in enumerate(valor):
                _conferir(sub, f"{origem}.{chave}[{i}]")
        elif chave in ("if", "then"):
            _conferir(valor, f"{origem}.{chave}")


def carregar(nome: str) -> dict:
    """Schema do contrato `nome` (arquivo schemas/<nome>.json). Cacheado; conferido ao carregar."""
    if nome not in _cache:
        caminho = PASTA / f"{nome}.json"
        if not caminho.is_file():
            raise ContratoDesconhecido(nome)
        with open(caminho, encoding="utf-8") as f:
            schema = json.load(f)
        _conferir(schema, nome)
        _cache[nome] = schema
    return _cache[nome]


def catalogo() -> dict:
    """schemas/catalogo.json: para cada contrato, a constante de versao (em const.py), a rota e a secao do docs."""
    with open(PASTA / "catalogo.json", encoding="utf-8") as f:
        return json.load(f)["contratos"]


def nomes() -> list[str]:
    """Todos os contratos que tem arquivo de schema (exceto o catalogo)."""
    return sorted(p.stem for p in PASTA.glob("*.json") if p.stem != "catalogo")


# ------------------------------------------------------------------ validacao
def _tipo_ok(valor, tipo: str) -> bool:
    if tipo == "object":
        return isinstance(valor, dict)
    if tipo == "array":
        return isinstance(valor, list)
    if tipo == "string":
        return isinstance(valor, str)
    if tipo == "boolean":
        return isinstance(valor, bool)
    if tipo == "null":
        return valor is None
    if tipo == "integer":
        return isinstance(valor, int) and not isinstance(valor, bool)
    # number
    return isinstance(valor, (int, float)) and not isinstance(valor, bool) and math.isfinite(valor)


def _igual(a, b) -> bool:
    """Igualdade estrita de tipo: True nao e 1, 1.0 nao e "1"."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return type(a) is type(b) and a == b


def validar(schema, valor, caminho: str = "$") -> list[str]:
    """Lista de erros (vazia = valido). Cada erro e 'caminho: regra', sem o valor recebido."""
    if schema is True or schema == {}:
        return []
    if schema is False:
        return [f"{caminho}: nao permitido"]
    erros: list[str] = []

    if "type" in schema:
        tipos = [schema["type"]] if isinstance(schema["type"], str) else schema["type"]
        if not any(_tipo_ok(valor, t) for t in tipos):
            return [f"{caminho}: tipo esperado {'|'.join(tipos)}"]   # os demais testes assumem o tipo certo
    if "const" in schema and not _igual(valor, schema["const"]):
        erros.append(f"{caminho}: valor diferente do esperado")
    if "enum" in schema and not any(_igual(valor, e) for e in schema["enum"]):
        erros.append(f"{caminho}: valor fora do conjunto permitido")

    if isinstance(valor, dict):
        for campo in schema.get("required", []):
            if campo not in valor:
                erros.append(f"{caminho}.{campo}: obrigatorio")
        for campo, sub in schema.get("properties", {}).items():
            if campo in valor:
                erros += validar(sub, valor[campo], f"{caminho}.{campo}")
    elif isinstance(valor, list):
        if "minItems" in schema and len(valor) < schema["minItems"]:
            erros.append(f"{caminho}: itens a menos")
        if "items" in schema:
            for i, item in enumerate(valor):
                erros += validar(schema["items"], item, f"{caminho}[{i}]")
    elif isinstance(valor, str):
        if "minLength" in schema and len(valor) < schema["minLength"]:
            erros.append(f"{caminho}: texto curto demais")
        if "maxLength" in schema and len(valor) > schema["maxLength"]:
            erros.append(f"{caminho}: texto longo demais")
        if "pattern" in schema and re.search(schema["pattern"], valor) is None:
            erros.append(f"{caminho}: formato invalido")
    elif isinstance(valor, (int, float)) and not isinstance(valor, bool):
        if "minimum" in schema and valor < schema["minimum"]:
            erros.append(f"{caminho}: abaixo do minimo")
        if "maximum" in schema and valor > schema["maximum"]:
            erros.append(f"{caminho}: acima do maximo")

    for sub in schema.get("allOf", []):
        erros += validar(sub, valor, caminho)
    if "anyOf" in schema and not any(not validar(sub, valor, caminho) for sub in schema["anyOf"]):
        erros.append(f"{caminho}: nenhuma alternativa aceita")
    if "if" in schema and not validar(schema["if"], valor, caminho) and "then" in schema:
        erros += validar(schema["then"], valor, caminho)
    return erros
