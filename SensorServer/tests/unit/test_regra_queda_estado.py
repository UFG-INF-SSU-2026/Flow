"""
regra_queda: maquina do gatilho A e abertura de episodios.

CARACTERIZACAO: fixa o comportamento atual do codigo. Se uma regra mudar, o teste muda de proposito.
Todos os instantes sao passados em `agora`; nao ha sleep.
"""
import const
import regra_queda

U = 1
T0 = 1000.0


def wokwi(value, vmax):
    return {"eventType": "imobilidade.leitura", "value": value, "valueMax": vmax}


def app(m, mmax):
    return {"acelerometro": {"magnitude": m, "magnitude_max_janela": mmax}}


IMPACTO = wokwi(3.0, 25.0)      # din_max 25 >= 20 (impacto wokwi)
CALMA = wokwi(0.3, 0.4)         # din_max 0.4 < 2 (repouso wokwi)
APP_IMPACTO = app(20.0, 25.81)  # din_max ~16 >= 15 (impacto app)
APP_CALMA = app(9.81, 10.0)     # din_max ~0.19 < 1.5 (repouso app)


def avaliar(t, dados, fonte="wokwi", uid=U, device=None):
    return regra_queda.avaliar(uid, fonte, dados, agora=t, device_id=device)


def fase(uid=U):
    return regra_queda._estados[uid].fase


# ---------------------------- gatilho A ---------------------------------------
class TestGatilhoA:
    def test_impacto_mais_repouso_abre_o_episodio(self, ler_confirmacoes):
        assert avaliar(T0, IMPACTO) is None
        assert fase() == "POS_IMPACTO"
        assert avaliar(T0 + 1, CALMA) is None
        assert avaliar(T0 + 2, CALMA) is None
        pedido = avaliar(T0 + 3, CALMA)                      # T_POS_IMPACTO_S = 3
        assert isinstance(pedido, regra_queda.PedidoConfirmacao)
        assert pedido.motivo == const.MOTIVO_A
        assert pedido.timeout_s == const.TIMEOUT_CONFIRMACAO_S
        assert fase() == "OBSERVANDO"
        ep = regra_queda._episodios[U]
        assert ep.episode_id == pedido.episode_id
        assert ep.prazo == T0 + 3 + const.TIMEOUT_CONFIRMACAO_S + const.MARGEM_SERVIDOR_S

        (aberto,) = [l for l in ler_confirmacoes(U) if l["tipo"] == "episodio.aberto"]
        assert aberto["episodeId"] == pedido.episode_id and aberto["motivo"] == const.MOTIVO_A
        assert aberto["fonte_do_disparo"] == "wokwi" and aberto["versao_referencia"] == 1
        assert aberto["fontes_usadas"][0]["fonte"] == "wokwi"

    def test_limiares_do_app(self):
        avaliar(T0, APP_IMPACTO, "app")
        assert fase() == "POS_IMPACTO"
        avaliar(T0 + 1, APP_CALMA, "app")
        avaliar(T0 + 2, APP_CALMA, "app")
        pedido = avaliar(T0 + 3, APP_CALMA, "app")
        assert pedido is not None and pedido.motivo == const.MOTIVO_A

    def test_cada_leitura_usa_o_limiar_da_propria_fonte(self):
        avaliar(T0, wokwi(3.0, 16.0))                    # 16 < 20: nao e impacto no wokwi
        assert fase() == "OBSERVANDO"
        avaliar(T0 + 1, APP_IMPACTO, "app")              # ~16 >= 15: e impacto no app
        assert fase() == "POS_IMPACTO"

    def test_pico_da_janela_cancela_mesmo_com_ultima_amostra_calma(self):
        avaliar(T0, IMPACTO)
        avaliar(T0 + 1, CALMA)
        assert avaliar(T0 + 2, wokwi(0.3, 5.0)) is None  # value calmo, valueMax 5 >= 2
        assert fase() == "OBSERVANDO"
        for dt in (3, 4, 5, 6):
            assert avaliar(T0 + dt, CALMA) is None       # impacto cancelado: nada abre

    def test_movimento_de_outra_fonte_tambem_cancela(self):
        avaliar(T0, IMPACTO)                             # impacto no wokwi
        avaliar(T0 + 1, app(9.81, 12.0), "app")          # app: din_max ~2.2 >= 1.5 (movimento)
        assert fase() == "OBSERVANDO"
        for dt in (2, 3, 4, 5):
            assert avaliar(T0 + dt, CALMA) is None

    def test_novo_impacto_na_leitura_de_movimento_recomeca_a_contagem(self):
        avaliar(T0, IMPACTO)
        avaliar(T0 + 2, IMPACTO)                         # movimento e impacto de novo: t_impacto = T0+2
        assert fase() == "POS_IMPACTO"
        assert avaliar(T0 + 3, CALMA) is None            # 3 s do primeiro impacto, so 1 s do segundo
        assert avaliar(T0 + 4, CALMA) is None
        assert avaliar(T0 + 5, CALMA) is not None        # 3 s do segundo

    def test_silencio_maior_que_o_intervalo_descarta_o_impacto(self):
        avaliar(T0, IMPACTO)
        assert avaliar(T0 + 3.5, CALMA) is None          # passou de INTERVALO_MAX_ENTRE_LEITURAS_S (3)
        assert fase() == "OBSERVANDO"
        for dt in (4.5, 5.5, 6.5, 7.5):
            assert avaliar(T0 + dt, CALMA) is None

    def test_lacuna_de_exatamente_o_intervalo_maximo_ainda_vale(self):
        avaliar(T0, IMPACTO)
        assert avaliar(T0 + 3.0, CALMA) is not None      # 3.0 nao e "mais que" 3

    def test_leitura_invalida_nao_move_a_maquina(self):
        avaliar(T0, IMPACTO)
        assert avaliar(T0 + 1, {"eventType": "imobilidade.leitura", "value": -5.0}) is None
        assert fase() == "POS_IMPACTO"


# ---------------------------- episodios ---------------------------------------
class TestEpisodios:
    def test_episodio_aberto_bloqueia_novo(self):
        assert regra_queda.abrir_por_imobilidade(U, agora=T0) is not None
        assert regra_queda.abrir_por_imobilidade(U, agora=T0 + 1) is None

    def test_gatilho_b_com_a_aberto_devolve_none(self):
        avaliar(T0, IMPACTO)
        avaliar(T0 + 1, CALMA)
        avaliar(T0 + 2, CALMA)
        assert avaliar(T0 + 3, CALMA) is not None        # A abriu
        assert regra_queda.abrir_por_imobilidade(U, agora=T0 + 3) is None

    def test_gatilho_b_usa_o_motivo_b(self):
        pedido = regra_queda.abrir_por_imobilidade(U, agora=T0)
        assert pedido.motivo == const.MOTIVO_B

    def test_cooldown_conta_do_encerramento(self):
        regra_queda.abrir_por_imobilidade(U, agora=T0)
        prazo = const.TIMEOUT_CONFIRMACAO_S + const.MARGEM_SERVIDOR_S
        assert [e.resultado for e in regra_queda.vigiar(agora=T0 + prazo)] == ["EXPIRADO"]
        fim = T0 + prazo
        assert regra_queda.abrir_por_imobilidade(U, agora=fim + const.COOLDOWN_S - 1) is None

    def test_depois_do_cooldown_abre_outro(self):
        primeiro = regra_queda.abrir_por_imobilidade(U, agora=T0)
        fim = T0 + const.TIMEOUT_CONFIRMACAO_S + const.MARGEM_SERVIDOR_S
        regra_queda.vigiar(agora=fim)
        segundo = regra_queda.abrir_por_imobilidade(U, agora=fim + const.COOLDOWN_S)
        assert segundo is not None and segundo.episode_id != primeiro.episode_id

    def test_usuarios_sao_independentes(self):
        assert regra_queda.abrir_por_imobilidade(1, agora=T0) is not None
        assert regra_queda.abrir_por_imobilidade(2, agora=T0) is not None
