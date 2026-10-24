"""
A analise que AGE pelo banco, a entrega de push da borda por cursor e o pipeline da borda (via_banco).
Banco falso (sem Docker), relogio explicito e ciclos SINCRONOS. A prova ponta a ponta (os cenarios de simular_cenarios.py pelo
banco, com as rotas reais) esta em tests/integration/test_acoes_banco_cenarios.py.
"""
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from analise import const as const_analise
from analise import laco, nucleo, regra_queda
from borda import entrega_push, via_banco
from borda import const as const_borda
from comum import const as const_comum
from comum import mensagens
from flow_db.fake import BancoFalso

T0 = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)


class Relogio:
    def __init__(self):
        self.agora = T0

    def __call__(self):
        return self.agora

    def ir_para(self, segundos: float):
        self.agora = T0 + timedelta(seconds=segundos)


def leitura_wokwi(t, seq, value, vmax=None, user_id=1):
    p = {"schemaVersion": const_comum.SCHEMA_VERSION_WOKWI, "eventType": "imobilidade.leitura", "deviceId": "bridge-dev",
         "entityId": "idoso-sim", "eventTimeMs": seq * 1000, "sequence": seq, "value": value, "unit": "m/s2",
         "state": "NORMAL"}
    if vmax is not None:
        p["valueMax"] = vmax
    return {"instante_ingestao": T0 + timedelta(seconds=t), "user_id": user_id, "device_id": "bridge-dev",
            "fonte": "wokwi", "tipo_evento": "imobilidade.leitura", "schema_version": const_comum.SCHEMA_VERSION_WOKWI,
            "instante_origem": None, "conta_id": "bridge1", "payload": p}


def queda_wokwi():
    """Leitura normal, impacto e T_POS_IMPACTO_S s de repouso: o pedido sai na ultima leitura (t = 4)."""
    linhas = [leitura_wokwi(0, 1, 1.0, 1.5), leitura_wokwi(1, 2, 3.0, 25.0)]
    return linhas + [leitura_wokwi(2 + k, 3 + k, 0.3, 0.4) for k in range(int(const_analise.T_POS_IMPACTO_S))]


def resposta(t, message_id, user_id=1, tipo="confirmacao.resposta", state="ESTOU_BEM"):
    ev = {"eventType": tipo, "state": state, "messageId": message_id, "device_id": "cel1", "source": "app"}
    return {"instante_ingestao": T0 + timedelta(seconds=t), "user_id": user_id, "device_id": "cel1", "fonte": "app",
            "tipo": tipo, "message_id": message_id, "schema_version": const_comum.SCHEMA_VERSION_APP, "payload": ev}


class Mundo:
    def __init__(self):
        self.rel = Relogio()
        self.bd = BancoFalso(relogio=self.rel)
        self.analise, self.borda = self.bd.analise(), self.bd.borda()
        self.laco = laco.LacoAnalise(obter_repo=lambda: self.analise, relogio=self.rel, ao_decidir=lambda *_: None,
                                     agir=True)

    def t(self, nome):
        return self.bd.tabelas[nome]

    def comandos(self, tipo=None):
        return [c for c in self.t("comandos_push") if tipo is None or c["tipo"] == tipo]

    def pedido_na_analise(self):
        """Queda ja decidida (t = 4)."""
        self.borda.gravar_leituras(queda_wokwi())
        self.rel.ir_para(4)
        self.laco.executar_ciclo()
        return self.comandos(mensagens.TIPO_SOLICITADA)[0]


@pytest.fixture
def m():
    return Mundo()


# ============================================================================ a analise que age
class TestLacoAgindo:
    def test_queda_grava_pedido_episodio_confirmacao_estado_e_cursor_juntos(self, m):
        pedido = m.pedido_na_analise()
        assert (pedido["canal"], pedido["tipo"], pedido["user_id"]) == ("todos", mensagens.TIPO_SOLICITADA, 1)
        assert pedido["expira_em"] == T0 + timedelta(seconds=4 + const_comum.TIMEOUT_CONFIRMACAO_S
                                                     + const_comum.MARGEM_SERVIDOR_S)
        (ep,) = m.t("episodios")
        assert (ep["estado"], ep["tipo"], ep["device_id"], ep["episodio_id"]) == (
            "aguardando_confirmacao", const_analise.MOTIVO_A, "bridge-dev", pedido["episodio_id"])
        assert [c["tipo"] for c in m.t("confirmacoes")] == ["episodio.aberto"]
        (est,) = m.t("estado_dispositivo")
        assert (est["device_id"], est["estado"]["estado_atual"]) == ("bridge-dev", "NORMAL")
        assert m.bd.cursor_analise["ultimo_leitura_id"] == 5
        assert m.bd.cursor_analise["versao_codigo"] == const_analise.VERSAO_CODIGO_ACOES

    def test_alerta_de_imobilidade_e_pedido_pelo_gatilho_b(self, m):
        decisao = leitura_wokwi(0, 1, 0.3)
        decisao["payload"].update(eventType="imobilidade.decisao", state="ALERTA_IMOBILIDADE")
        decisao["tipo_evento"] = "imobilidade.decisao"
        m.borda.gravar_leituras([decisao])
        m.rel.ir_para(1)
        m.laco.executar_ciclo()
        (alerta,) = m.t("alertas")
        assert alerta["tipo"] == "imobilidade" and alerta["device_id"] == "bridge-dev"
        assert m.comandos(mensagens.TIPO_SOLICITADA)[0]["payload"]["payload"]["motivo"] == const_analise.MOTIVO_B

    def test_resposta_estou_bem_encerra_como_confirmado(self, m):
        pedido = m.pedido_na_analise()
        m.borda.gravar_resposta(resposta(6, pedido["message_id"]))
        m.rel.ir_para(7)
        m.laco.executar_ciclo()
        (enc,) = m.comandos(mensagens.TIPO_ENCERRADA)
        assert enc["payload"]["payload"]["resultado"] == "CONFIRMADO" and enc["episodio_id"] == pedido["episodio_id"]
        (ep,) = m.t("episodios")
        assert (ep["estado"], ep["motivo_encerramento"], ep["tipo"]) == ("encerrado", "CONFIRMADO", const_analise.MOTIVO_A)
        tipos = [c["tipo"] for c in m.t("confirmacoes")]
        assert tipos == ["episodio.aberto", "confirmacao.resposta", "episodio.encerrado"]

    def test_resposta_de_outro_usuario_nao_encerra_o_episodio_ds1(self, m):
        pedido = m.pedido_na_analise()
        m.borda.gravar_resposta(resposta(6, pedido["message_id"], user_id=2))
        m.rel.ir_para(7)
        m.laco.executar_ciclo()
        assert not m.comandos(mensagens.TIPO_ENCERRADA)
        (ep,) = m.t("episodios")
        assert ep["estado"] == "aguardando_confirmacao"
        desconhecida = [c for c in m.t("confirmacoes") if c["dados"].get("mensagem_desconhecida")]
        assert len(desconhecida) == 1 and desconhecida[0]["user_id"] == 2

    def test_vigia_grava_o_encerramento_expirado(self, m):
        pedido = m.pedido_na_analise()
        m.rel.ir_para(4 + const_comum.TIMEOUT_CONFIRMACAO_S + const_comum.MARGEM_SERVIDOR_S + 1)
        m.laco.executar_ciclo()
        (enc,) = m.comandos(mensagens.TIPO_ENCERRADA)
        assert enc["payload"]["payload"]["resultado"] == "EXPIRADO" and enc["episodio_id"] == pedido["episodio_id"]
        (ep,) = m.t("episodios")
        assert (ep["estado"], ep["motivo_encerramento"]) == ("encerrado", "EXPIRADO")
        assert m.t("confirmacoes")[-1]["tipo"] == "episodio.encerrado"

    def test_falha_do_banco_no_vigia_guarda_o_encerramento_e_regrava_depois(self, m, monkeypatch):
        m.pedido_na_analise()
        original, estado = m.analise.transacao, {"falhar": True}

        @contextmanager
        def transacao():
            if estado["falhar"]:
                raise ConnectionError("banco fora")
            with original() as tx:
                yield tx

        monkeypatch.setattr(m.analise, "transacao", transacao)
        m.rel.ir_para(13)
        m.laco.executar_ciclo()                      # o vigia encerra no estado das regras, mas a gravacao falha
        assert not m.comandos(mensagens.TIPO_ENCERRADA)
        estado["falhar"] = False
        m.rel.ir_para(14)
        m.laco.executar_ciclo()                      # nada novo a decidir: so reaplica o pendente
        assert len(m.comandos(mensagens.TIPO_ENCERRADA)) == 1
        m.rel.ir_para(15)
        m.laco.executar_ciclo()
        assert len(m.comandos(mensagens.TIPO_ENCERRADA)) == 1       # e uma vez so

    def test_falha_no_meio_do_ciclo_desfaz_tudo_e_a_nova_tentativa_nao_duplica(self, m, monkeypatch):
        m.borda.gravar_leituras(queda_wokwi())
        m.rel.ir_para(4)
        original, estado = m.analise.gravar_comando, {"falhar": True}

        def gravar_comando(c):
            if estado["falhar"]:
                raise ConnectionError("banco fora")
            return original(c)

        monkeypatch.setattr(m.analise, "gravar_comando", gravar_comando)
        with pytest.raises(ConnectionError):
            m.laco.executar_ciclo()
        assert all(not m.t(t) for t in ("comandos_push", "episodios", "confirmacoes", "estado_dispositivo"))
        assert m.bd.cursor_analise["ultimo_leitura_id"] == 0           # o cursor tambem foi desfeito

        estado["falhar"] = False
        m.laco.executar_ciclo()
        assert len(m.comandos(mensagens.TIPO_SOLICITADA)) == 1 and len(m.t("episodios")) == 1
        assert [c["tipo"] for c in m.t("confirmacoes")] == ["episodio.aberto"]      # nao duplicou
        assert len(m.t("estado_dispositivo")) == 1                                  # o rollback nao o marcou como gravado
        assert m.bd.cursor_analise["ultimo_leitura_id"] == 5

    def test_linha_antiga_so_reconstroi_o_estado_e_nao_age(self, m):
        m.borda.gravar_leituras(queda_wokwi())
        m.rel.ir_para(4 + const_analise.IDADE_MAX_ACAO_S + 1)
        m.laco.executar_ciclo()
        assert all(not m.t(t) for t in ("comandos_push", "alertas", "confirmacoes", "episodios", "estado_dispositivo"))
        assert m.bd.cursor_analise["ultimo_leitura_id"] == 5           # o cursor avancou: nao ficou preso no passado
        assert m.bd.cursor_analise["metricas"][laco.M_DECISOES_ANTIGAS] > 0

    def test_estado_so_e_regravado_quando_a_assinatura_muda(self, m, monkeypatch):
        gravacoes = []
        original = m.analise.gravar_estado_dispositivo
        monkeypatch.setattr(m.analise, "gravar_estado_dispositivo", lambda s: (gravacoes.append(s), original(s)))
        m.borda.gravar_leituras([leitura_wokwi(0, 1, 1.0, 1.5)])
        m.rel.ir_para(1)
        m.laco.executar_ciclo()
        m.borda.gravar_leituras([leitura_wokwi(1, 2, 1.0, 1.5)])
        m.rel.ir_para(2)
        m.laco.executar_ciclo()
        assert len(gravacoes) == 1

    def test_sem_agir_o_laco_continua_so_registrando(self):
        mundo = Mundo()
        mundo.laco = laco.LacoAnalise(obter_repo=lambda: mundo.analise, relogio=mundo.rel, ao_decidir=lambda *_: None)
        mundo.borda.gravar_leituras(queda_wokwi())
        mundo.rel.ir_para(4)
        mundo.laco.executar_ciclo()
        assert all(not mundo.t(t) for t in ("comandos_push", "alertas", "confirmacoes", "episodios"))
        assert mundo.bd.cursor_analise["versao_codigo"] == const_analise.VERSAO_CODIGO


# ============================================================================ a entrega da borda
class CanalFalso:
    def __init__(self, nome, resultado="ENTREGUE_AO_CANAL", falha=None):
        self.CANAL, self.resultado, self.falha, self.enviados = nome, resultado, falha, []

    def enviar(self, user_id, mensagem):
        if self.falha:
            raise self.falha
        self.enviados.append((user_id, mensagem))
        return self.resultado


def comando_na_analise(m, t=0.0, encerramento=False):
    if encerramento:
        cmd = nucleo.comando_de_encerramento(regra_queda.Encerramento(1, "ep-x", "EXPIRADO"), T0 + timedelta(seconds=t))
    else:
        pedido = regra_queda.PedidoConfirmacao("ep-x", const_comum.TIMEOUT_CONFIRMACAO_S, const_analise.MOTIVO_A)
        cmd = nucleo.comando_de_pedido(nucleo.novo_estado(), 1, pedido, "bridge-dev", T0 + timedelta(seconds=t))
    m.analise.gravar_comando(cmd.como_linha())
    return cmd


class TestEntregaPush:
    def _entrega(self, m, *canais):
        canais = list(canais) or [CanalFalso("app"), CanalFalso("wokwi")]
        return entrega_push.EntregaPush(obter_repo=lambda: m.borda, relogio=m.rel, canais=canais), canais

    def test_entrega_a_todos_os_canais_grava_entregas_com_comando_id_e_avanca_o_cursor(self, m):
        cmd = comando_na_analise(m)
        ent, (app, wokwi) = self._entrega(m)
        m.rel.ir_para(1)
        assert ent.executar_ciclo() == 1
        assert app.enviados == [(1, cmd.mensagem)] and wokwi.enviados == [(1, cmd.mensagem)]
        entregas = m.t("entregas_push")
        assert {(e["canal"], e["resultado"], e["comando_id"], e["message_id"]) for e in entregas} == {
            ("app", "enviado", 1, cmd.mensagem["messageId"]), ("wokwi", "enviado", 1, cmd.mensagem["messageId"])}
        assert m.bd.cursor_borda["ultimo_comando_id"] == 1 and m.bd.cursor_borda["ultimo_comando_instante"] == T0

    def test_o_comando_nao_e_entregue_duas_vezes(self, m):
        comando_na_analise(m)
        ent, (app, _w) = self._entrega(m)
        m.rel.ir_para(1)
        ent.executar_ciclo()
        assert ent.executar_ciclo() == 0 and len(app.enviados) == 1 and len(m.t("entregas_push")) == 2

    def test_entrega_na_ordem_dos_ids_e_inclui_o_encerramento(self, m):
        comando_na_analise(m, 0)
        comando_na_analise(m, 3, encerramento=True)
        ent, (app, _w) = self._entrega(m)
        m.rel.ir_para(4)
        assert ent.executar_ciclo() == 2
        assert [msg["messageType"] for _u, msg in app.enviados] == [mensagens.TIPO_SOLICITADA, mensagens.TIPO_ENCERRADA]

    def test_sem_conexao_vira_resultado_sem_conexao(self, m):
        comando_na_analise(m)
        ent, _ = self._entrega(m, CanalFalso("app", "SEM_CANAL_ATIVO"), CanalFalso("wokwi", "TIPO_NAO_SUPORTADO"))
        m.rel.ir_para(1)
        ent.executar_ciclo()
        assert {(e["canal"], e["resultado"]) for e in m.t("entregas_push")} == {("app", "sem_conexao"),
                                                                              ("wokwi", "nao_suportado")}

    def test_falha_de_um_canal_nao_impede_o_outro(self, m):
        comando_na_analise(m)
        ent, (_a, wokwi) = self._entrega(m, CanalFalso("app", falha=OSError("x")), CanalFalso("wokwi"))
        m.rel.ir_para(1)
        ent.executar_ciclo()
        resultados = {e["canal"]: e["resultado"] for e in m.t("entregas_push")}
        assert resultados == {"app": "erro", "wokwi": "enviado"} and len(wokwi.enviados) == 1

    def test_comando_vencido_nao_e_enviado_e_a_entrega_diz_expirado(self, m):
        comando_na_analise(m, 0)
        ent, (app, wokwi) = self._entrega(m)
        m.rel.ir_para(const_comum.TIMEOUT_CONFIRMACAO_S + const_comum.MARGEM_SERVIDOR_S + 1)
        assert ent.executar_ciclo() == 1
        assert not app.enviados and not wokwi.enviados
        assert {e["resultado"] for e in m.t("entregas_push")} == {"expirado"}
        assert m.bd.cursor_borda["ultimo_comando_id"] == 1

    def test_historico_mais_velho_que_a_janela_nem_e_lido(self, m):
        comando_na_analise(m, 0)
        ent, (app, _w) = self._entrega(m)
        m.rel.ir_para(const_borda.JANELA_COMANDOS_S + 1)
        assert ent.executar_ciclo() == 0 and not app.enviados and not m.t("entregas_push")

    def test_falha_ao_gravar_o_cursor_reentrega_no_proximo_ciclo(self, m, monkeypatch):
        comando_na_analise(m)
        ent, (app, _w) = self._entrega(m)
        original, estado = m.borda.gravar_cursor_borda, {"falhar": True}

        def gravar_cursor(*a):
            if estado["falhar"]:
                raise ConnectionError("banco fora")
            return original(*a)

        monkeypatch.setattr(m.borda, "gravar_cursor_borda", gravar_cursor)
        m.rel.ir_para(1)
        with pytest.raises(ConnectionError):
            ent.executar_ciclo()
        estado["falhar"] = False
        ent.executar_ciclo()
        assert len(app.enviados) == 2            # ao menos uma vez: o dispositivo ignora o messageId repetido
        assert ent.executar_ciclo() == 0

    def test_sem_banco_nao_faz_nada(self):
        assert entrega_push.EntregaPush(obter_repo=lambda: None).executar_ciclo() == 0

    def test_a_borda_so_escreve_entregas_e_o_seu_cursor(self, m):
        """Quem entrega nao decide: nada de comandos, alertas, confirmacoes ou episodios sai da borda."""
        comando_na_analise(m)
        ent, _ = self._entrega(m)
        m.rel.ir_para(1)
        ent.executar_ciclo()
        assert len(m.t("comandos_push")) == 1 and not m.t("alertas") and not m.t("confirmacoes") and not m.t("episodios")

    def test_a_thread_entrega_e_para(self, monkeypatch):
        monkeypatch.setattr(const_borda, "INTERVALO_LEITURA_COMANDOS_S", 0.01)
        bd = BancoFalso()
        app = CanalFalso("app")
        ent = entrega_push.EntregaPush(obter_repo=lambda: bd.borda(), canais=[app])
        pedido = regra_queda.PedidoConfirmacao("ep-t", const_comum.TIMEOUT_CONFIRMACAO_S, const_analise.MOTIVO_A)
        cmd = nucleo.comando_de_pedido(nucleo.novo_estado(), 1, pedido, "d", datetime.now(timezone.utc))
        t = ent.iniciar()
        bd.analise().gravar_comando(cmd.como_linha())
        fim = time.monotonic() + 3
        while not app.enviados and time.monotonic() < fim:
            time.sleep(0.01)
        ent.parar()
        assert len(app.enviados) == 1 and not t.is_alive()


# ============================================================================ o pipeline da borda
class TestViaBanco:
    def test_sobe_a_entrega(self, monkeypatch):
        class Falsa:
            def iniciar(self):
                return threading.current_thread()

            def parar(self, timeout):
                pass

        monkeypatch.setattr(via_banco, "_entrega", None)
        monkeypatch.setattr(via_banco.entrega_push, "EntregaPush", Falsa)
        assert via_banco.iniciar_entrega() is threading.current_thread()
        via_banco.parar(1.0)

    def test_estado_do_dispositivo_vem_do_banco(self, banco):
        assert via_banco.estado_do_dispositivo("bridge-dev") is None
        resumo = {"device_id": "bridge-dev", "user_id": 1, "estado_atual": "NORMAL", "ultima_decisao": None,
                  "alerta_ativo": False}
        banco.analise().gravar_estado_dispositivo({"user_id": 1, "device_id": "bridge-dev", "atualizado_em": T0,
                                                   "versao_regra": 3, "estado": resumo})
        assert via_banco.estado_do_dispositivo("bridge-dev") == resumo
