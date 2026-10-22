"""
Transporte INTERNO dos cenarios da S3b (simular_cenarios.py): a mesma lista de passos que o script roda contra o
servidor real roda aqui sem rede e sem Docker, em tempo VIRTUAL.

  - o servidor e o do teste (Flask test_client, banco falso); o caminho direto age como sempre;
  - o relogio e o da fixture `relogio` (congelado): `esperar_ate` o avanca em passos de PASSO_S e, a cada passo, faz o
    que as threads de fundo fariam (descarga das leituras, vigia do caminho direto a cada 1 s, um ciclo do laco);
  - `Sombra` recebe as decisoes dos dois lados, com o MESMO relogio virtual em `visto_em` (entao o atraso medido aqui e
    o do modelo, nao o do hardware: o atraso real se mede rodando o simular_cenarios.py).
"""
from analise import laco, sombra
from borda import caminho_direto, server
from comum import mensagens

PASSO_S = 0.25
PASSOS_POR_VIGIA = 4        # o vigia do caminho direto roda a cada 1 s (const_analise.INTERVALO_VIGIA_S)


class TransporteInterno:
    def __init__(self, relogio, cli, tokens: dict, banco, lc: "laco.LacoAnalise"):
        self.relogio, self.cli, self.tokens, self.banco, self.laco = relogio, cli, tokens, banco, lc
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
        """O messageId do ultimo pedido que o CAMINHO DIRETO gravou (e o que o dispositivo teria recebido)."""
        pedidos = [l for l in self.banco.tabelas["comandos_push"] if l["tipo"] == mensagens.TIPO_SOLICITADA]
        return pedidos[-1]["message_id"] if pedidos else None

    # ---- o que as threads de fundo fariam ----
    def _passo(self) -> None:
        self._passos += 1
        server._descarregar_leituras()
        if self._passos % PASSOS_POR_VIGIA == 0:
            caminho_direto._vigia_iteracao()
        self.laco.executar_ciclo()


def montar_laco(relogio, banco, coleta: "sombra.Sombra", traduzir: bool = True) -> "laco.LacoAnalise":
    """Laco do teste: banco falso, relogio virtual, e a sombra ouvindo (`traduzir=False` mostra o que acontece sem a
    traducao dos messageId: as confirmacoes divergem)."""
    return laco.LacoAnalise(
        obter_repo=lambda: banco.analise(), relogio=relogio.agora_utc, ao_decidir=lambda *_: None,
        ao_observar=lambda d, origem, rec: coleta.registrar(sombra.LADO_SOMBRA, d, origem, rec),
        traduzir_resposta=coleta.traduzir_resposta if traduzir else None)
