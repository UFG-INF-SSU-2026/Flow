"""
Regra de lacunas de id (funcao pura, sem banco e sem relogio proprio: quem chama informa `agora`).

Colunas IDENTITY reservam o id no INSERT, nao no COMMIT. Por isso um leitor que ve o id 101 pode ainda nao ver o 100
(commit fora de ordem), e um ROLLBACK deixa buracos que nunca serao preenchidos. Os dois casos sao indistinguiveis na
hora da leitura; a regra usa um prazo: buraco "recente" e esperado, buraco "antigo" e tratado como permanente.

Dado um lote lido em ordem de id (id > depois_de_id), percorre procurando o primeiro buraco (esperado = anterior + 1;
o primeiro esperado e depois_de_id + 1):
  - sem buraco                                  -> devolve o lote inteiro;
  - buraco e a linha DEPOIS dele e mais nova que `espera_s` -> devolve so o trecho antes do buraco (talvez vazio),
    sem avancar alem dele; a proxima passada le de novo;
  - buraco e a linha depois dele tem `espera_s` ou mais     -> buraco permanente: segue, somando o tamanho em
    `lacunas_ignoradas`.
"""
from datetime import datetime


def aplicar_regra_lacunas(linhas: list[dict], depois_de_id: int, coluna_instante: str, agora: datetime,
                          espera_s: float) -> tuple[list[dict], int]:
    """Devolve (linhas_seguras, lacunas_ignoradas). `linhas` precisa estar em ordem crescente de id."""
    esperado = depois_de_id + 1
    ignoradas = 0
    for i, linha in enumerate(linhas):
        if linha["id"] != esperado:
            if (agora - linha[coluna_instante]).total_seconds() < espera_s:
                return linhas[:i], ignoradas       # buraco recente: espera
            ignoradas += linha["id"] - esperado    # buraco permanente: conta e segue
        esperado = linha["id"] + 1
    return linhas, ignoradas
