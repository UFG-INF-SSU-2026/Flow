"""
Interface da camada de acesso da BORDA (papel flow_borda). Quem fala com o dispositivo so conhece isto, nunca SQL.

Escreve: leituras, respostas_dispositivo, entregas_push, auditoria, telemetria (legado), cursor_borda.
Le:      comandos_push, alertas, episodios, estado_dispositivo, saude_pipeline (so leitura).

Convencoes: instantes sao datetime com fuso (UTC); dicts de entrada tem as chaves das colunas (ver pg_borda.py);
`leituras` deve ser gravada por UMA unica thread (a de descarga), em lotes.
"""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Sequence


class RepoBorda(ABC):
    @abstractmethod
    def gravar_leituras(self, leituras: Sequence[dict]) -> int:
        """Um lote numa transacao so. Chaves: instante_ingestao, user_id, device_id, fonte, tipo_evento, schema_version,
        instante_origem, conta_id, payload. Devolve quantas linhas gravou."""

    @abstractmethod
    def gravar_telemetria(self, linhas: Sequence[dict]) -> int:
        """Legado (dupla escrita). Chaves: instante, user_id, app, wokwi."""

    @abstractmethod
    def gravar_resposta(self, resposta: dict) -> None:
        """Chaves: instante_ingestao, user_id, device_id, fonte, tipo, message_id, schema_version, payload."""

    @abstractmethod
    def gravar_entrega(self, entrega: dict) -> None:
        """Chaves: instante, comando_id, message_id, user_id, device_id, canal, resultado, detalhe."""

    @abstractmethod
    def gravar_auditoria(self, registro: dict) -> None:
        """Chaves: instante, rota, status, motivo, conta, ip. Nunca o conteudo da requisicao."""

    @abstractmethod
    def ler_comandos_novos(self, depois_de_id: int, instante_minimo: datetime | None = None,
                           limite: int = 100) -> list[dict]:
        """Comandos de push com id > depois_de_id, em ordem de id."""

    @abstractmethod
    def ler_cursor_borda(self) -> dict:
        """{ultimo_comando_id, ultimo_comando_instante, atualizado_em}."""

    @abstractmethod
    def gravar_cursor_borda(self, ultimo_comando_id: int, ultimo_comando_instante: datetime | None) -> None:
        ...

    @abstractmethod
    def consultar_leituras(self, user_id: int, limite: int = 100) -> list[dict]:
        """Mais recentes primeiro (consulta administrativa)."""

    @abstractmethod
    def listar_alertas(self, user_id: int, limite: int = 50) -> list[dict]:
        ...

    @abstractmethod
    def buscar_episodio(self, episodio_id: str) -> dict | None:
        ...

    @abstractmethod
    def ler_estado_dispositivo(self, user_id: int, device_id: str) -> dict | None:
        ...

    @abstractmethod
    def ler_saude(self) -> dict:
        """{atraso_ids, idade_batimento_analise_s, idade_ultimo_comando_entregue_s} (None quando ainda nao ha dado)."""

    def fechar(self) -> None:
        pass
