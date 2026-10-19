"""
Pacote comum de banco do Flow (PostgreSQL + TimescaleDB).

Dois servicos falam com o banco, cada um com o seu papel e a sua camada de acesso:

    borda   -> RepoBorda   (repo_borda.py),   implementado por PgBorda   (pg_borda.py)   e FachadaBorda   (fake.py)
    analise -> RepoAnalise (repo_analise.py), implementado por PgAnalise (pg_analise.py) e FachadaAnalise (fake.py)

Este pacote NAO importa Flask nem nenhum modulo do SensorServer. O driver (psycopg) so e importado dentro das
funcoes que o usam, para que o banco falso (testes sem Docker) funcione sem ele.

Comandos (a partir de SensorServer/):
    python -m flow_db provisionar   # cria/ajusta os papeis flow_borda e flow_analise (usa o usuario administrativo)
    python -m flow_db migrar        # aplica as migracoes pendentes (usa o usuario administrativo)
    python -m flow_db status        # mostra versao aplicada e papeis
"""
