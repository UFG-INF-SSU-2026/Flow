"""
Relogio do ponto de ENTRADA do sistema (modulo folha: nao importa nada do projeto).

Roadmap do split, principio 3: as regras nao leem o relogio; recebem o instante como argumento. Quem le o relogio
e so quem esta na borda do fluxo:

  - a borda, UMA vez por requisicao, ao aceitar o dado (esse e o `instante_ingestao` gravado em `leituras` e
    o instante que o nucleo das regras usa);
  - o vigia que expira episodios (unico passo que depende do tempo que passa, nao do tempo dos dados).

Os testes congelam este relogio com `monkeypatch.setattr(tempo, "agora_utc", ...)` (fixture `relogio`).
Leia sempre `tempo.agora_utc()` na hora do uso, nunca `from tempo import agora_utc`.
"""
from datetime import datetime, timezone


def agora_utc() -> datetime:
    """Instante atual, com fuso UTC."""
    return datetime.now(timezone.utc)


def agora() -> float:
    """Mesmo instante, em segundos desde a epoca (a unidade das regras)."""
    return agora_utc().timestamp()
