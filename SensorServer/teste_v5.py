"""
teste_v5.py - Verificacao manual V5 (operacao FORA DA PERMISSAO) do projeto Flow.

Autenticar nao basta: cada conta so pode o que o seu papel permite, e so sobre os user_ids do seu token. Dois casos:

  1. O cuidador do user_id 1 pede o historico de OUTRA pessoa (/cuidador/2/historico)
        -> 403 fora_de_escopo   (o papel pode ler, mas nao esta pessoa)
  2. Uma conta de papel idoso tenta rota de ADMIN (GET /dados)
        -> 403 sem_permissao    (o papel nao tem "dados:ler")

Preparacao: banco e borda no ar; contas cuidador1 e idoso1 existentes (INSTRUCOES.md, 1.3). A analise nao e necessaria.

Como rodar (na pasta SensorServer/, junto com v_comum.py):
    $env:FLOW_CUIDADOR_SECRET="<senha do cuidador1>"; $env:FLOW_IDOSO_SECRET="<senha do idoso1>"
    python teste_v5.py
Sem as variaveis, o script pergunta as senhas (sem mostrar).
"""
import tempfile
from pathlib import Path

import requests

import v_comum as c

DEVICE_CUIDADOR = "script-v5-cuidador"
DEVICE_IDOSO = "script-v5-idoso"


def mostrar(titulo: str, r: requests.Response, codigo: int, motivo: str) -> bool:
    ok = r.status_code == codigo and motivo in r.text
    print(f"\n=== {titulo} ===\nHTTP {r.status_code}  corpo: {r.text.strip()}")
    print(f"esperado: {codigo} {motivo}  ->  {'OK' if ok else 'DIFERENTE (confira o log [authz] da borda)'}")
    return ok


def main() -> None:
    tok_cuid = c.login("cuidador1", c.senha("FLOW_CUIDADOR_SECRET", "Senha de cuidador1: "), "app", DEVICE_CUIDADOR)
    tok_idoso = c.login("idoso1", c.senha("FLOW_IDOSO_SECRET", "Senha de idoso1: "), "app", DEVICE_IDOSO)
    com_acesso = tok_cuid["user_ids"]
    outro = max(com_acesso) + 1                       # um user_id que o token do cuidador NAO lista

    with tempfile.TemporaryDirectory(prefix="flow_v5_") as pasta:
        # O cuidador, como o app, apresenta certificado de cliente: sem ele a borda responderia 401 antes de checar o escopo.
        cert = c.pedir_certificado(tok_cuid["access_token"], DEVICE_CUIDADOR, Path(pasta), "cuid")

        # Caso 1: pessoa fora do escopo do token do cuidador
        r1 = c.cuidador_le("historico", outro, tok_cuid["access_token"], cert)
        ok1 = mostrar(f"Caso 1: cuidador (user_ids {com_acesso}) pedindo /cuidador/{outro}/historico", r1, 403,
                      "fora_de_escopo")

        # Caso 2: papel idoso em rota de admin. A checagem de PERMISSAO vem antes da de certificado: nao precisa de cert.
        r2 = requests.get(f"{c.BASE}/dados", headers={"Authorization": f"Bearer {tok_idoso['access_token']}"},
                          verify=c.CA, timeout=15)
        ok2 = mostrar("Caso 2: conta idoso em GET /dados (rota de admin)", r2, 403, "sem_permissao")

    print("\n=== Resumo V5 ===")
    print(f"cuidador de outra pessoa : {'OK' if ok1 else 'DIFERENTE'}")
    print(f"idoso em rota de admin   : {'OK' if ok2 else 'DIFERENTE'}")
    print("Evidencia: esta saida e as linhas '[authz] negado 403 ...' do terminal da borda.")


if __name__ == "__main__":
    main()
