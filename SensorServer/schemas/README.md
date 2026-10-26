# Contratos do SensorServer (fonte unica)

Cada arquivo `<contrato>.json` e um schema (subconjunto de JSON Schema 2020-12, ver `comum/schemas.py`). Estes arquivos
sao a **fonte unica** da estrutura dos dados; `docs/schemas.md` e a documentacao humana e e conferida contra eles por
teste (`tests/unit/test_contratos.py`). `catalogo.json` liga cada contrato a constante de versao em `const.py`, a rota e
a secao do `docs/schemas.md`.

## O que o schema valida (e o que nao)

- **Estrutura**: campos obrigatorios, tipos, vocabularios (`eventType` e `state`), tamanhos. E o que a **borda** confere
  (`borda/validacao.py`) antes de aceitar o dado.
- **Semantica** (faixa fisica, sentinelas, plausibilidade, regras): fica na **analise**. Por isso `value` e so `number`
  no schema; o teto de 160 m/s2 e regra.
- Propriedades desconhecidas sao sempre aceitas: um campo novo de um produtor mais novo nao quebra o servidor.
- Mensagens de erro trazem so o caminho e a regra, nunca o valor recebido.

## Politica de versao

- Campo **opcional** novo: **nao** muda a versao (basta acrescentar a propriedade ao schema).
- Mudanca **incompativel** (campo obrigatorio novo, tipo ou vocabulario alterado, campo removido): **aumenta** a versao.
  Sobe a constante `SCHEMA_VERSION_*` em `comum/const.py`, o `x-versao` do schema e a tabela de `docs/schemas.md`, e acrescenta
  uma linha ao backlog da secao. O teste de conferencia falha enquanto os tres nao concordarem.
- Versoes anteriores (>= `SCHEMA_VERSION_MINIMA`) continuam aceitas e sao validadas pelo schema atual; por isso a
  evolucao entre versoes aceitas so pode acrescentar campos opcionais.
- `x-versao` ausente = contrato sem versao (registros de saida do servidor, alertas, envelope, roteamento).

## Fora desta pasta

O comando serial servidor -> firmware (`CMD;BUZZER_ON;...`, secao 3 do `docs/schemas.md`) nao e JSON e nao tem schema.
