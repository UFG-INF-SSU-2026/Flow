"""
Trava de isolamento de segredos entre os dois servicos (segredos por dono).

Cada servico tem o seu `.env`, mas o ambiente do processo tambem herda variaveis do terminal. Para o isolamento nao
depender de disciplina, os pontos de entrada chamam `exigir_ambiente(servico)` na subida: se o ambiente trouxer um
segredo que NAO e do servico, ele se recusa a subir. A mensagem lista so os NOMES das variaveis, nunca os valores.

  borda    nao pode ver a senha do papel da analise nem a senha administrativa do banco
  analise  nao pode ver o segredo JWT, a senha do papel da borda, a senha administrativa nem os arquivos de contas
           (chave AES: a analise nem importa borda/crypto_utils.py, conferido por tests/unit/test_fronteiras.py)

Modulo folha (so a biblioteca padrao).
"""
import os
from typing import Mapping

PROIBIDAS: dict[str, tuple[str, ...]] = {
    "borda": ("FLOW_DB_ANALISE_PASSWORD", "FLOW_DB_PASSWORD"),
    "analise": ("FLOW_JWT_SECRET", "FLOW_DB_BORDA_PASSWORD", "FLOW_DB_PASSWORD", "FLOW_CONTAS_PATH",
                "FLOW_SESSOES_PATH"),
}


class AmbienteInvalido(RuntimeError):
    """O ambiente do processo traz segredo de outro dono. A mensagem nunca contem valores."""


def variaveis_proibidas(servico: str, ambiente: Mapping[str, str] | None = None) -> list[str]:
    """Nomes das variaveis proibidas que estao definidas (e nao vazias) no ambiente."""
    if servico not in PROIBIDAS:
        raise ValueError(f"servico desconhecido: {servico!r}")
    amb = os.environ if ambiente is None else ambiente
    return [nome for nome in PROIBIDAS[servico] if amb.get(nome)]


def exigir_ambiente(servico: str, ambiente: Mapping[str, str] | None = None) -> None:
    """Levanta AmbienteInvalido se o ambiente tiver segredo que nao e de `servico`."""
    achadas = variaveis_proibidas(servico, ambiente)
    if achadas:
        raise AmbienteInvalido(
            f"o servico '{servico}' nao pode enxergar {', '.join(achadas)} (segredo de outro dono). Remova-a do "
            f"ambiente do terminal e do .env.{servico}; cada servico tem o seu arquivo (ver .env.{servico}.example).")
