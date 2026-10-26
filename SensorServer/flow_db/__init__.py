"""
Camada de banco do Flow (PostgreSQL). Quem fala com o banco e a borda (Servidor 1), por uma unica interface:

    repo.py     a interface `Repo` (o que a borda precisa gravar e ler)
    pg.py       `Pg`, a implementacao sobre PostgreSQL
    fake.py     `BancoFalso`, a mesma interface em memoria (testes e `FLOW_DB_FAKE=1`, sem Docker)

Este pacote NAO importa Flask nem nenhum modulo do SensorServer alem de `comum`. O driver (psycopg) so e importado dentro
das funcoes que o usam, para que o banco falso funcione sem ele.

Comandos (a partir de SensorServer/):
    python -m flow_db migrar    # aplica as migracoes pendentes
    python -m flow_db status    # mostra a versao aplicada
"""
