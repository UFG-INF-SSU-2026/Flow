"""
Transporte INTERNO dos cenarios (simular_cenarios.py): a mesma lista de passos que o script roda contra a borda e a
analise reais roda aqui sem rede e sem Docker, em tempo VIRTUAL.

  - a borda e a do teste (Flask test_client, banco falso);
  - o relogio e o da fixture `relogio` (congelado): `esperar_ate` o avanca em passos de PASSO_S e, a cada passo, faz o
    que as threads de fundo fariam (descarga das leituras, um ciclo do laco, o vigia a cada 1 s, um ciclo da entrega).
"""
from borda import server
from comum import mensagens

PASSO_S = 0.25
PASSOS_POR_VIGIA = 4        # o vigia roda a cada 1 s


class TransporteInterno:
    def __init__(self, relogio, cli, tokens: dict, banco, lc, entrega=None):
        """`lc`: o LacoAnalise do teste; `entrega` (opcional): a `EntregaPush` da borda, um ciclo por passo."""
        self.relogio, self.cli, self.tokens, self.banco, self.laco = relogio, cli, tokens, banco, lc
        self.entrega = entrega
        self.t0 = relogio.agora()
        self._passos = 0

    # ---- interface de simular_cenarios.Sessao ----
    def iniciar_cenario(self) -> None:
        self.t0 = self.relogio.agora()

    def postar(self, rota: str, fonte: str, corpo: dict) -> int:
        return self.cli.post(rota, corpo, self.tokens[fonte]).status_code

    def esperar_ate(self, t_rel: float) -> None:
        alvo = self.t0 + t_rel
        while self.relogio.agora() < alvo:
            self.relogio.avancar(min(PASSO_S, alvo - self.relogio.agora()))
            self._passo()

    def ultimo_message_id(self) -> str | None:
        """O messageId do ultimo pedido gravado em `comandos_push` (o que o dispositivo teria recebido)."""
        pedidos = [l for l in self.banco.tabelas["comandos_push"] if l["tipo"] == mensagens.TIPO_SOLICITADA]
        return pedidos[-1]["message_id"] if pedidos else None

    # ---- o que as threads de fundo fariam ----
    def _passo(self) -> None:
        self._passos += 1
        server._descarregar_leituras()
        self.laco.executar_ciclo()
        if self._passos % PASSOS_POR_VIGIA == 0:
            self.laco.passo_vigia()
        if self.entrega is not None:
            self.entrega.executar_ciclo()
