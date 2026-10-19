"""
Interface da camada de acesso da ANALISE (papel flow_analise).

Le:      leituras, respostas_dispositivo, entregas_push (so leitura; cursor por id).
Escreve: comandos_push, alertas, confirmacoes, episodios, estado_dispositivo, cursor_analise.

`transacao()` agrupa varias escritas numa transacao so (processar um lote e avancar o cursor = tudo ou nada).
"""
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from datetime import datetime


class RepoAnalise(ABC):
    @abstractmethod
    def ler_leituras_novas(self, depois_de_id: int, instante_minimo: datetime | None = None,
                           limite: int = 500) -> list[dict]:
        """Leituras com id > depois_de_id em ordem de id. `instante_minimo` limita a busca aos chunks recentes."""

    @abstractmethod
    def ler_respostas_novas(self, depois_de_id: int, instante_minimo: datetime | None = None,
                            limite: int = 500) -> list[dict]:
        ...

    @abstractmethod
    def ler_entregas_novas(self, depois_de_id: int, instante_minimo: datetime | None = None,
                           limite: int = 500) -> list[dict]:
        ...

    @abstractmethod
    def gravar_comando(self, comando: dict) -> int:
        """Chaves: criado_em, user_id, device_id, canal, message_id, tipo, payload, expira_em, episodio_id,
        leitura_id_origem. Devolve o id."""

    @abstractmethod
    def gravar_alerta(self, alerta: dict) -> int:
        """Chaves: instante, user_id, device_id, tipo, mensagem, dados, episodio_id, leitura_id_origem, schema_version."""

    @abstractmethod
    def gravar_confirmacao(self, confirmacao: dict) -> int:
        """Chaves: instante, user_id, tipo, dados, episodio_id, message_id, leitura_id_origem, schema_version."""

    @abstractmethod
    def gravar_episodio(self, episodio: dict) -> None:
        """Upsert por episodio_id. Chaves: episodio_id, user_id, device_id, tipo, estado, aberto_em, atualizado_em,
        encerrado_em, motivo_encerramento, dados. No conflito: atualiza estado/atualizado_em, preserva encerrado_em e
        motivo ja gravados quando o novo vier vazio, e funde `dados`."""

    @abstractmethod
    def listar_episodios_abertos(self) -> list[dict]:
        """Estado diferente de 'encerrado' (reconstroi o estado ao reiniciar)."""

    @abstractmethod
    def gravar_estado_dispositivo(self, estado: dict) -> None:
        """Upsert por (user_id, device_id). Chaves: user_id, device_id, atualizado_em, versao_regra, estado."""

    @abstractmethod
    def ler_cursor(self) -> dict:
        ...

    @abstractmethod
    def gravar_cursor(self, ultimo_leitura_id: int | None = None, ultimo_leitura_instante: datetime | None = None,
                      ultimo_resposta_id: int | None = None, ultimo_entrega_id: int | None = None,
                      versao_codigo: str | None = None, metricas: dict | None = None) -> None:
        """Avanca o que for informado (None = mantem) e SEMPRE renova atualizado_em (o batimento)."""

    @abstractmethod
    def transacao(self) -> AbstractContextManager["RepoAnalise"]:
        """with repo.transacao() as tx: ... commit ao sair; qualquer excecao desfaz tudo."""

    def fechar(self) -> None:
        pass
