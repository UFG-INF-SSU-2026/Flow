"""regra_queda: retorno do dispositivo (CONFIRMADO, tardia, mensagem desconhecida) e vigia (EXPIRADO, poda)."""
import const
import mensagens
import regra_queda

T0 = 1000.0
PRAZO = const.TIMEOUT_CONFIRMACAO_S + const.MARGEM_SERVIDOR_S


def abrir(uid=1, t=T0):
    """Abre um episodio (gatilho B) e registra a messageId do pedido, como o server.py faz."""
    pedido = regra_queda.abrir_por_imobilidade(uid, agora=t)
    assert pedido is not None
    msg = mensagens.criar_pedido_confirmacao(uid, pedido.episode_id, pedido.timeout_s, pedido.motivo)
    regra_queda.anotar_mensagem(uid, pedido.episode_id, msg["messageId"], agora=t)
    return pedido, msg["messageId"]


def retorno(mid, tipo="confirmacao.resposta", state="ESTOU_BEM", source="app"):
    return {"eventType": tipo, "state": state, "messageId": mid, "device_id": "cel1", "source": source}


class TestRetorno:
    def test_estou_bem_conhecido_e_aberto_encerra_como_confirmado(self, ler_confirmacoes):
        pedido, mid = abrir()
        enc = regra_queda.registrar_retorno(1, retorno(mid), agora=T0 + 2)
        assert enc == regra_queda.Encerramento(1, pedido.episode_id, "CONFIRMADO")
        ep = regra_queda._episodios[1]
        assert ep.resultado == "CONFIRMADO" and ep.encerrado_em == T0 + 2

        linhas = ler_confirmacoes(1)
        assert [l["tipo"] for l in linhas] == ["episodio.aberto", "confirmacao.resposta", "episodio.encerrado"]
        resposta = linhas[1]
        assert resposta["episodeId"] == pedido.episode_id
        assert "tardia" not in resposta and "mensagem_desconhecida" not in resposta
        assert linhas[2]["resultado"] == "CONFIRMADO"

    def test_toda_linha_tem_timestamp_e_tipo(self, ler_confirmacoes):
        _, mid = abrir()
        regra_queda.registrar_retorno(1, retorno(mid), agora=T0 + 1)
        assert all("timestamp" in l and "tipo" in l for l in ler_confirmacoes(1))

    def test_sem_resposta_so_grava(self, ler_confirmacoes):
        pedido, mid = abrir()
        enc = regra_queda.registrar_retorno(1, retorno(mid, state="SEM_RESPOSTA"), agora=T0 + 1)
        assert enc is None
        assert regra_queda._episodios[1].encerrado_em is None
        ultima = ler_confirmacoes(1)[-1]
        assert ultima["tipo"] == "confirmacao.resposta" and ultima["state"] == "SEM_RESPOSTA"
        assert ultima["episodeId"] == pedido.episode_id

    def test_push_ack_so_grava(self, ler_confirmacoes):
        pedido, mid = abrir()
        enc = regra_queda.registrar_retorno(1, retorno(mid, "push.ack", "ATIVADO", "wokwi"), agora=T0 + 1)
        assert enc is None
        assert regra_queda._episodios[1].encerrado_em is None
        ultima = ler_confirmacoes(1)[-1]
        assert ultima["tipo"] == "push.ack" and ultima["episodeId"] == pedido.episode_id

    def test_message_id_desconhecida(self, ler_confirmacoes):
        abrir()
        enc = regra_queda.registrar_retorno(1, retorno("nao-existe"), agora=T0 + 1)
        assert enc is None
        assert regra_queda._episodios[1].encerrado_em is None
        ultima = ler_confirmacoes(1)[-1]
        assert ultima["episodeId"] is None and ultima["mensagem_desconhecida"] is True

    def test_message_id_de_outro_usuario(self, ler_confirmacoes):
        _, mid = abrir(uid=1)
        enc = regra_queda.registrar_retorno(2, retorno(mid), agora=T0 + 1)
        assert enc is None
        assert regra_queda._episodios[1].encerrado_em is None
        (linha,) = ler_confirmacoes(2)
        assert linha["episodeId"] is None and linha["mensagem_desconhecida"] is True

    def test_retorno_depois_de_confirmado_e_tardio(self, ler_confirmacoes):
        _, mid = abrir()
        regra_queda.registrar_retorno(1, retorno(mid), agora=T0 + 1)
        assert regra_queda.registrar_retorno(1, retorno(mid), agora=T0 + 2) is None
        assert ler_confirmacoes(1)[-1]["tardia"] is True

    def test_retorno_depois_de_expirado_e_tardio_e_nao_reabre(self, ler_confirmacoes):
        pedido, mid = abrir()
        regra_queda.vigiar(agora=T0 + PRAZO)
        assert regra_queda.registrar_retorno(1, retorno(mid), agora=T0 + PRAZO + 1) is None
        ultima = ler_confirmacoes(1)[-1]
        assert ultima["tardia"] is True and ultima["episodeId"] == pedido.episode_id
        assert regra_queda._episodios[1].resultado == "EXPIRADO"

    def test_retorno_de_episodio_anterior_ao_mais_recente_e_tardio(self, ler_confirmacoes):
        _, mid1 = abrir(t=T0)
        regra_queda.vigiar(agora=T0 + PRAZO)
        segundo, _ = abrir(t=T0 + PRAZO + const.COOLDOWN_S)
        enc = regra_queda.registrar_retorno(1, retorno(mid1), agora=T0 + PRAZO + const.COOLDOWN_S + 1)
        assert enc is None
        assert ler_confirmacoes(1)[-1]["tardia"] is True
        assert regra_queda._episodios[1].episode_id == segundo.episode_id
        assert regra_queda._episodios[1].encerrado_em is None       # o episodio novo segue aberto


class TestVigia:
    def test_expira_so_depois_de_timeout_mais_margem(self):
        pedido, _ = abrir()
        assert regra_queda.vigiar(agora=T0 + PRAZO - 0.01) == []
        assert regra_queda.vigiar(agora=T0 + PRAZO) == [regra_queda.Encerramento(1, pedido.episode_id, "EXPIRADO")]
        assert regra_queda.vigiar(agora=T0 + PRAZO + 1) == []        # ja encerrado: nao repete

    def test_grava_episodio_encerrado(self, ler_confirmacoes):
        pedido, _ = abrir()
        regra_queda.vigiar(agora=T0 + PRAZO)
        ultima = ler_confirmacoes(1)[-1]
        assert ultima["tipo"] == "episodio.encerrado" and ultima["resultado"] == "EXPIRADO"
        assert ultima["episodeId"] == pedido.episode_id

    def test_falha_ao_gravar_um_encerramento_nao_impede_os_outros(self, monkeypatch, ler_confirmacoes):
        abrir(uid=1)
        abrir(uid=2)
        original = regra_queda.registrar

        def registrar_com_falha(uid, tipo, dados, agora=None):
            if uid == 1 and tipo == "episodio.encerrado":
                raise OSError("disco cheio")
            return original(uid, tipo, dados, agora)

        monkeypatch.setattr(regra_queda, "registrar", registrar_com_falha)
        enc = regra_queda.vigiar(agora=T0 + PRAZO)
        assert {e.user_id for e in enc} == {1, 2}
        assert regra_queda._episodios[1].resultado == "EXPIRADO"     # ja marcado antes de gravar
        assert ler_confirmacoes(2)[-1]["tipo"] == "episodio.encerrado"
        assert [l["tipo"] for l in ler_confirmacoes(1)] == ["episodio.aberto"]

    def test_poda_o_mapa_depois_da_retencao(self, monkeypatch):
        monkeypatch.setattr(const, "RETENCAO_MENSAGENS_S", 100)
        _, mid = abrir()                                             # a mensagem expira em T0 + PRAZO
        regra_queda.vigiar(agora=T0 + PRAZO)
        assert mid in regra_queda._mensagens
        regra_queda.vigiar(agora=T0 + PRAZO + 100)                   # corte = T0 + PRAZO: ainda nao e menor
        assert mid in regra_queda._mensagens
        regra_queda.vigiar(agora=T0 + PRAZO + 101)
        assert mid not in regra_queda._mensagens

    def test_resposta_depois_da_poda_vira_mensagem_desconhecida(self, monkeypatch, ler_confirmacoes):
        monkeypatch.setattr(const, "RETENCAO_MENSAGENS_S", 100)
        _, mid = abrir()
        regra_queda.vigiar(agora=T0 + PRAZO + 101)
        regra_queda.registrar_retorno(1, retorno(mid), agora=T0 + PRAZO + 102)
        ultima = ler_confirmacoes(1)[-1]
        assert ultima["episodeId"] is None and ultima["mensagem_desconhecida"] is True
