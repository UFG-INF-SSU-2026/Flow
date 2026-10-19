"""
Matriz de permissoes (roadmap, secao 6.2). Fonte unica em Python:
  - o banco falso (fake.py) a usa para RECUSAR escritor errado, sem Docker;
  - o teste de contrato (tests/banco) compara com os GRANTs reais do banco (migracao 0005).
Mudou aqui? Mude flow_db/migrations/0005_permissoes_politicas.sql junto (e vice-versa).

S = ler (SELECT)   I = inserir (INSERT)   U = atualizar (UPDATE)   D = apagar (DELETE)
"""
BORDA = "borda"
ANALISE = "analise"

MATRIZ: dict[str, dict[str, str]] = {
    # tabela:                 {papel: privilegios}
    "leituras":              {BORDA: "IS", ANALISE: "S"},
    "respostas_dispositivo": {BORDA: "I",  ANALISE: "S"},
    "entregas_push":         {BORDA: "I",  ANALISE: "S"},
    "comandos_push":         {BORDA: "S",  ANALISE: "IS"},
    "alertas":               {BORDA: "S",  ANALISE: "IS"},
    "confirmacoes":          {BORDA: "S",  ANALISE: "IS"},
    "episodios":             {BORDA: "S",  ANALISE: "ISU"},
    "estado_dispositivo":    {BORDA: "S",  ANALISE: "ISU"},
    "cursor_analise":        {BORDA: "S",  ANALISE: "ISU"},
    "cursor_borda":          {BORDA: "ISU"},
    "telemetria":            {BORDA: "IS"},          # legado (dupla escrita; sai na parte 2)
    "auditoria":             {BORDA: "IS"},
    "contas":                {BORDA: "ISU"},
    "sessoes":               {BORDA: "ISD"},
    "schema_migrations":     {BORDA: "S",  ANALISE: "S"},
    "saude_pipeline":        {BORDA: "S"},          # visao de saude do pipeline (migracao 0004)
}

PAPEL_PG = {BORDA: "flow_borda", ANALISE: "flow_analise"}
OPERACAO = {"S": "SELECT", "I": "INSERT", "U": "UPDATE", "D": "DELETE"}


class PermissaoNegada(PermissionError):
    """O papel nao tem o privilegio pedido na tabela (o banco real responderia 'permission denied')."""


def privilegios(papel: str, tabela: str) -> str:
    return MATRIZ.get(tabela, {}).get(papel, "")


def exigir(papel: str, tabela: str, op: str) -> None:
    if op not in privilegios(papel, tabela):
        raise PermissaoNegada(f"papel '{papel}' nao pode {OPERACAO[op]} em '{tabela}'")


def matriz_como_conjunto() -> set[tuple[str, str, str]]:
    """{(papel_pg, tabela, privilegio_sql)} - formato comparavel com information_schema.role_table_grants."""
    return {(PAPEL_PG[papel], tabela, OPERACAO[letra])
            for tabela, por_papel in MATRIZ.items() for papel, letras in por_papel.items() for letra in letras}
