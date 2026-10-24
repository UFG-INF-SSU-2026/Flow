"""
Interface unica de acesso ao banco. Quem usa so conhece isto, nunca SQL.

Convencoes: instantes sao datetime com fuso (UTC); os dicts de entrada tem as chaves das colunas (ver pg.py).
`transacao()` agrupa varias escritas numa so: tudo ou nada.
"""
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Sequence


class Repo(ABC):
    # ---- leituras ----
    @abstractmethod
    def gravar_leituras(self, leituras: Sequence[dict]) -> int:
        """Um lote numa transacao so. Chaves: instante_ingestao, user_id, device_id, fonte, tipo_evento, schema_version,
        instante_origem, conta_id, payload. Devolve quantas linhas gravou."""

    @abstractmethod
    def ler_leituras_recentes(self, desde: datetime, antes_de: datetime | None = None, limite: int = 5000) -> list[dict]:
        """As leituras mais recentes com instante_ingestao >= desde (e < antes_de, se dado), ate `limite` (as MAIS
        RECENTES, se houver mais), em ordem crescente de id. Serve a reconstruir o estado da analise."""

    # ---- decisoes ----
    @abstractmethod
    def gravar_comando(self, comando: dict) -> int | None:
        """Chaves: criado_em, user_id, device_id, canal, message_id, tipo, payload, expira_em, episodio_id. Devolve o id,
        ou None se o message_id ja existia (nada e regravado)."""

    @abstractmethod
    def gravar_alerta(self, alerta: dict) -> int:
        """Chaves: instante, user_id, device_id, tipo, mensagem, dados, episodio_id, schema_version."""

    @abstractmethod
    def gravar_confirmacao(self, confirmacao: dict) -> int:
        """Chaves: instante, user_id, tipo, dados, episodio_id, message_id, schema_version."""

    @abstractmethod
    def gravar_episodio(self, episodio: dict) -> None:
        """Upsert por episodio_id. Chaves: episodio_id, user_id, device_id, tipo, estado, aberto_em, atualizado_em,
        encerrado_em, motivo_encerramento, dados. No conflito: atualiza estado/atualizado_em, preserva encerrado_em e
        motivo ja gravados quando o novo vier vazio, e funde `dados`."""

    @abstractmethod
    def gravar_estado_dispositivo(self, estado: dict) -> None:
        """Upsert por (user_id, device_id). Chaves: user_id, device_id, atualizado_em, versao_regra, estado."""

    # ---- consultas ----
    @abstractmethod
    def ler_estado_por_dispositivo(self, device_id: str) -> dict | None:
        """O estado gravado para `device_id` (o atualizado por ultimo, se mais de um usuario o usar), ou None.
        A rota /estado/<device_id> nao sabe o usuario."""

    @abstractmethod
    def listar_episodios_recentes(self, desde: datetime) -> list[dict]:
        """Episodios abertos (de qualquer idade) e os atualizados desde `desde`, em ordem de aberto_em."""

    @abstractmethod
    def listar_comandos_de_episodios(self, episodio_ids: Sequence[str]) -> list[dict]:
        """Comandos de push dos episodios dados, em ordem de id (campos: id, episodio_id, message_id, tipo, criado_em,
        expira_em). Devolve os `message_id` que ligam a resposta do dispositivo ao episodio."""

    @abstractmethod
    def transacao(self) -> AbstractContextManager["Repo"]:
        """with repo.transacao() as tx: ... commit ao sair; qualquer excecao desfaz tudo."""

    def fechar(self) -> None:
        pass
