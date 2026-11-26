"""
teste_v3.py - Verificacao manual V3 (PERSISTENCIA) do projeto Flow, com apoio do banco.

O V3 pede: gerar dados, PARAR borda, analise e banco, subir de novo, e ver que leituras, episodios, contas e sessoes
continuam no banco e o app segue logado. Parar e subir processos e com voce; o script tira uma "foto" do banco ANTES e
compara DEPOIS. Ele consulta o PostgreSQL pelo proprio container (docker compose exec ... psql), entao nao precisa de senha.

Uso (em SensorServer/, junto com v_comum.py), na ordem:
    1. Gere dados: use o app, o Wokwi, ou  python simular_queda.py  (cria leituras e um episodio).
    2.   python teste_v3.py antes        # tira a foto e a salva em v3_antes.json
    3. Pare borda e analise (Ctrl+C), depois o banco:   docker compose stop db
    4. Suba tudo de novo:  docker compose start db   e   scripts/tudo.ps1   (ou .sh)
    5.   python teste_v3.py depois       # compara o banco com a foto
    6. No celular: confira que o app SEGUE LOGADO (parte manual).

Variaveis (padroes): FLOW_DB_ADMIN_USER=flow, FLOW_DB_NAME=flow (lidas do .env do SensorServer, via comum.const).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from comum import const as _carrega_env             # importar carrega SensorServer/.env para os.environ

ARQUIVO = Path("v3_antes.json")
USUARIO = os.environ.get("FLOW_DB_ADMIN_USER", "flow")
BANCO = os.environ.get("FLOW_DB_NAME", "flow")

# Tabelas conferidas: (nome na saida, SQL de contagem ATE o instante da foto). Dados com historico so crescem; contas nao
# mudam sozinhas. Contar "ate o instante da foto" evita contar o que chegou depois (o app continua enviando).
CONTAGENS = {
    "leituras":      "SELECT count(*) FROM leituras WHERE instante_ingestao <= '{t}'",
    "episodios":     "SELECT count(*) FROM episodios WHERE aberto_em <= '{t}'",
    "alertas":       "SELECT count(*) FROM alertas WHERE instante <= '{t}'",
    "confirmacoes":  "SELECT count(*) FROM confirmacoes WHERE instante <= '{t}'",
    "contas":        "SELECT count(*) FROM contas",
}


def psql(sql: str) -> str:
    """Roda uma consulta no container do banco e devolve o texto da primeira coluna da primeira linha."""
    p = subprocess.run(["docker", "compose", "exec", "-T", "db", "psql", "-U", USUARIO, "-d", BANCO, "-tA", "-c", sql],
                       capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit(f"Falha ao consultar o banco: {p.stderr.strip() or p.stdout.strip()}\n"
                 "O banco esta no ar? (docker compose ps) Rode este script em SensorServer/.")
    return p.stdout.strip().splitlines()[0] if p.stdout.strip() else ""


def foto(instante: str | None = None) -> dict:
    t = instante or psql("SELECT now()")
    dados = {nome: int(psql(sql.format(t=t))) for nome, sql in CONTAGENS.items()}
    dados["contas_ids"] = sorted(psql("SELECT string_agg(id, ',' ORDER BY id) FROM contas").split(","))
    dados["episodios_ids"] = sorted(filter(None, psql(
        f"SELECT string_agg(episodio_id::text, ',' ORDER BY episodio_id) FROM episodios WHERE aberto_em <= '{t}'").split(",")))
    dados["sessoes_agora"] = int(psql("SELECT count(*) FROM sessoes WHERE expira_em > now()"))
    dados["instante"] = t
    return dados


def main() -> None:
    modo = sys.argv[1] if len(sys.argv) > 1 else ""
    if modo == "antes":
        f = foto()
        ARQUIVO.write_text(json.dumps(f, indent=1), encoding="utf-8")
        print(f"Foto do banco salva em {ARQUIVO} (instante {f['instante']}):")
        for nome in CONTAGENS:
            print(f"  {nome:13} {f[nome]}")
        print(f"  sessoes ativas {f['sessoes_agora']}")
        if f["leituras"] == 0 or f["episodios"] == 0:
            print("AVISO: sem leituras ou sem episodio; gere dados (simular_queda.py) antes, senao o V3 nada prova.")
        print("\nAgora: pare borda e analise, depois 'docker compose stop db'; suba tudo de novo; rode 'depois'.")
    elif modo == "depois":
        if not ARQUIVO.exists():
            sys.exit("Rode 'python teste_v3.py antes' primeiro.")
        antes = json.loads(ARQUIVO.read_text(encoding="utf-8"))
        agora = foto(antes["instante"])               # conta de novo ATE o mesmo instante da foto anterior
        print(f"Comparando o banco com a foto de {antes['instante']}:\n")
        tudo_ok = True
        for nome in CONTAGENS:
            ok = agora[nome] == antes[nome]
            tudo_ok &= ok
            print(f"  {nome:13} antes={antes[nome]:6}  depois={agora[nome]:6}  {'OK' if ok else 'DIFERENTE'}")
        for nome in ("contas_ids", "episodios_ids"):
            faltam = set(antes[nome]) - set(agora[nome])
            tudo_ok &= not faltam
            print(f"  {nome:13} {'todos presentes' if not faltam else 'FALTAM: ' + str(sorted(faltam))}")
        print(f"  sessoes ativas antes={antes['sessoes_agora']} depois={agora['sessoes_agora']} "
              "(informativo: a sessao pode expirar ou ser renovada pelo app)")
        print("\nResultado do banco: " + ("OK, nada se perdeu." if tudo_ok else "DIFERENTE, ha perda de dados."))
        print("Falta a parte manual: o app SEGUE logado no celular?")
    else:
        sys.exit("Uso: python teste_v3.py antes | depois")


if __name__ == "__main__":
    main()
