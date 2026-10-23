"""Banco falso: contrato das duas camadas de acesso (sem Docker). O mesmo contrato roda contra o Postgres em tests/banco."""
from datetime import datetime, timedelta, timezone

import pytest

from flow_db import permissoes
from flow_db.fake import BancoFalso

T0 = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)


def leitura(user_id=1, seg=0, fonte="app", device="cel1"):
    return {"instante_ingestao": T0 + timedelta(seconds=seg), "user_id": user_id, "device_id": device, "fonte": fonte,
            "tipo_evento": "telemetria", "schema_version": 3, "instante_origem": None, "conta_id": "idoso1",
            "payload": {"x": seg}}


def comando(msg="m1", seg=0, **extra):
    return {"criado_em": T0 + timedelta(seconds=seg), "user_id": 1, "device_id": None, "canal": "todos",
            "message_id": msg, "tipo": "queda.confirmacao_solicitada", "payload": {"a": 1}, **extra}


@pytest.fixture
def bd():
    return BancoFalso(relogio=lambda: T0)


@pytest.fixture
def borda(bd):
    return bd.borda()


@pytest.fixture
def analise(bd):
    return bd.analise()


class TestPermissoes:
    def test_borda_nao_escreve_onde_so_a_analise_escreve(self, borda):
        for tabela, op in [("comandos_push", "I"), ("alertas", "I"), ("episodios", "I")]:
            with pytest.raises(permissoes.PermissaoNegada):
                borda._b._inserir("borda", tabela, {})

    def test_analise_nao_escreve_nas_tabelas_de_fronteira(self, bd, analise):
        for tabela in ("leituras", "respostas_dispositivo", "entregas_push", "auditoria"):
            with pytest.raises(permissoes.PermissaoNegada):
                bd._inserir("analise", tabela, {})

    def test_cursor_de_cada_servico_so_e_escrito_pelo_dono(self, bd):
        with pytest.raises(permissoes.PermissaoNegada):
            bd._cursor("borda", "cursor_analise", "U")
        with pytest.raises(permissoes.PermissaoNegada):
            bd._cursor("analise", "cursor_borda", "S")

    def test_ninguem_apaga_em_tabela_de_fronteira(self):
        for tabela in ("leituras", "respostas_dispositivo", "entregas_push", "comandos_push", "alertas", "episodios"):
            assert not any("D" in letras for letras in permissoes.MATRIZ[tabela].values())

    def test_cada_tabela_tem_um_unico_escritor_de_insercao(self):
        for tabela in ("leituras", "respostas_dispositivo", "entregas_push", "comandos_push", "alertas", "confirmacoes",
                       "episodios", "estado_dispositivo"):
            escritores = [p for p, letras in permissoes.MATRIZ[tabela].items() if set(letras) & {"I", "U"}]
            assert len(escritores) == 1, tabela


class TestLeiturasECursor:
    def test_gravar_leituras_atribui_ids_em_ordem(self, borda, analise):
        assert borda.gravar_leituras([leitura(seg=i) for i in range(3)]) == 3
        assert [l["id"] for l in analise.ler_leituras_novas(0)] == [1, 2, 3]

    def test_ler_novas_respeita_cursor_limite_e_instante_minimo(self, borda, analise):
        borda.gravar_leituras([leitura(seg=i * 60) for i in range(5)])
        assert [l["id"] for l in analise.ler_leituras_novas(2)] == [3, 4, 5]
        assert [l["id"] for l in analise.ler_leituras_novas(0, limite=2)] == [1, 2]
        assert [l["id"] for l in analise.ler_leituras_novas(0, instante_minimo=T0 + timedelta(seconds=120))] == [3, 4, 5]

    def test_lote_invalido_nao_grava_nada(self, borda, analise):
        with pytest.raises(ValueError):
            borda.gravar_leituras([leitura(seg=0), leitura(seg=1, fonte="outra")])
        assert analise.ler_leituras_novas(0) == []

    def test_leitura_devolvida_e_copia(self, borda, analise):
        borda.gravar_leituras([leitura()])
        analise.ler_leituras_novas(0)[0]["payload"]["x"] = 999
        assert analise.ler_leituras_novas(0)[0]["payload"]["x"] == 0

    def test_cursor_avanca_so_o_informado_e_renova_o_batimento(self, bd, analise):
        bd.relogio = lambda: T0 + timedelta(seconds=30)
        analise.gravar_cursor(ultimo_leitura_id=7)
        c = analise.ler_cursor()
        assert c["ultimo_leitura_id"] == 7 and c["ultimo_resposta_id"] == 0
        assert c["atualizado_em"] == T0 + timedelta(seconds=30)
        analise.gravar_cursor(metricas={"n": 1})
        assert analise.ler_cursor()["ultimo_leitura_id"] == 7        # None = mantem

    def test_respostas_e_entregas_novas(self, borda, analise):
        borda.gravar_resposta({"instante_ingestao": T0, "user_id": 1, "device_id": "cel1", "fonte": "app",
                               "tipo": "push.ack", "message_id": "m1", "schema_version": 3, "payload": {}})
        borda.gravar_entrega({"instante": T0, "message_id": "m1", "user_id": 1, "canal": "app", "resultado": "enviado"})
        assert [r["message_id"] for r in analise.ler_respostas_novas(0)] == ["m1"]
        assert [e["canal"] for e in analise.ler_entregas_novas(0)] == ["app"]

    def test_entrega_com_resultado_invalido_e_recusada(self, borda):
        with pytest.raises(ValueError):
            borda.gravar_entrega({"instante": T0, "user_id": 1, "canal": "app", "resultado": "talvez"})


class TestComandosAlertasEpisodios:
    def test_borda_le_comandos_da_analise_em_ordem_de_id(self, borda, analise):
        ids = [analise.gravar_comando(comando(f"m{i}", seg=i)) for i in range(3)]
        assert ids == [1, 2, 3]
        assert [c["message_id"] for c in borda.ler_comandos_novos(1)] == ["m1", "m2"]

    def test_alerta_e_confirmacao_tem_id(self, analise, borda):
        a = analise.gravar_alerta({"instante": T0, "user_id": 1, "tipo": "imobilidade", "mensagem": "x", "dados": {}})
        c = analise.gravar_confirmacao({"instante": T0, "user_id": 1, "tipo": "episodio.aberto", "dados": {}})
        assert (a, c) == (1, 1)
        assert [x["tipo"] for x in borda.listar_alertas(1)] == ["imobilidade"]

    def _ep(self, **extra):
        base = {"episodio_id": "e1", "user_id": 1, "device_id": None, "tipo": "IMOBILIDADE_PROLONGADA",
                "estado": "aguardando_confirmacao", "aberto_em": T0, "atualizado_em": T0, "encerrado_em": None,
                "motivo_encerramento": None, "dados": {"timeout_s": 5}}
        base.update(extra)
        return base

    def test_upsert_de_episodio_preserva_abertura_e_funde_dados(self, analise, borda):
        analise.gravar_episodio(self._ep(device_id="cel1"))
        fim = T0 + timedelta(seconds=9)
        analise.gravar_episodio(self._ep(tipo="DESCONHECIDO", estado="encerrado", atualizado_em=fim, encerrado_em=fim,
                                         motivo_encerramento="CONFIRMADO", device_id=None, dados={"extra": 1}))
        ep = borda.buscar_episodio("e1")
        assert ep["estado"] == "encerrado" and ep["motivo_encerramento"] == "CONFIRMADO" and ep["encerrado_em"] == fim
        assert ep["tipo"] == "IMOBILIDADE_PROLONGADA" and ep["device_id"] == "cel1"      # nao sobrescritos
        assert ep["dados"] == {"timeout_s": 5, "extra": 1}

    def test_encerramento_sem_abertura_previa_cria_o_episodio(self, analise, borda):
        analise.gravar_episodio(self._ep(tipo="DESCONHECIDO", estado="encerrado", encerrado_em=T0,
                                         motivo_encerramento="EXPIRADO"))
        assert borda.buscar_episodio("e1")["motivo_encerramento"] == "EXPIRADO"

    def test_episodios_abertos_exclui_os_encerrados(self, analise):
        analise.gravar_episodio(self._ep(episodio_id="a"))
        analise.gravar_episodio(self._ep(episodio_id="b", estado="encerrado", encerrado_em=T0))
        assert [e["episodio_id"] for e in analise.listar_episodios_abertos()] == ["a"]

    def test_estado_do_dispositivo_e_upsert(self, analise, borda):
        base = {"user_id": 1, "device_id": "d", "atualizado_em": T0, "versao_regra": 1, "estado": {"alerta_ativo": False}}
        analise.gravar_estado_dispositivo(base)
        analise.gravar_estado_dispositivo({**base, "estado": {"alerta_ativo": True}})
        assert len(borda._b.tabelas["estado_dispositivo"]) == 1
        assert borda.ler_estado_dispositivo(1, "d")["estado"] == {"alerta_ativo": True}
        assert borda.ler_estado_dispositivo(1, "outro") is None

    def test_comando_com_canal_invalido_e_recusado(self, analise):
        with pytest.raises(ValueError):
            analise.gravar_comando(comando(canal="sms"))


class TestTransacao:
    def test_commit_normal_grava_tudo(self, analise, borda):
        with analise.transacao() as tx:
            tx.gravar_comando(comando("m1"))
            tx.gravar_alerta({"instante": T0, "user_id": 1, "tipo": "t", "dados": {}})
        assert len(borda.ler_comandos_novos(0)) == 1 and len(borda.listar_alertas(1)) == 1

    def test_excecao_desfaz_tudo_e_os_ids(self, analise, borda):
        with pytest.raises(RuntimeError):
            with analise.transacao() as tx:
                tx.gravar_comando(comando("m1"))
                tx.gravar_cursor(ultimo_leitura_id=50)
                raise RuntimeError("falhou no meio do lote")
        assert borda.ler_comandos_novos(0) == [] and analise.ler_cursor()["ultimo_leitura_id"] == 0
        assert analise.gravar_comando(comando("m2")) == 1        # a sequencia tambem foi restaurada


class TestSaudeEAuditoria:
    def test_saude_com_cursores_novos_o_atraso_e_o_total(self, borda):
        borda.gravar_leituras([leitura(seg=i) for i in range(4)])
        s = borda.ler_saude()
        assert s["atraso_ids"] == 4 and s["idade_ultimo_comando_entregue_s"] is None

    def test_saude_acompanha_cursores(self, bd, borda, analise):
        borda.gravar_leituras([leitura(seg=i) for i in range(4)])
        analise.gravar_cursor(ultimo_leitura_id=3)
        bd.relogio = lambda: T0 + timedelta(seconds=12)
        borda.gravar_cursor_borda(1, T0 + timedelta(seconds=2))
        s = borda.ler_saude()
        assert s["atraso_ids"] == 1
        assert s["idade_batimento_analise_s"] == 12 and s["idade_ultimo_comando_entregue_s"] == 10

    def test_auditoria_e_telemetria_sao_so_da_borda(self, borda):
        borda.gravar_auditoria({"instante": T0, "rota": "/dados", "status": 401, "motivo": "token_ausente"})
        borda.gravar_telemetria([{"instante": T0, "user_id": 1, "app": {"a": 1}, "wokwi": None}])
        assert len(borda._b.tabelas["auditoria"]) == 1 and len(borda._b.tabelas["telemetria"]) == 1


class TestLeiturasDaRecuperacao:
    """S6: o mesmo contrato que tests/banco/test_contrato_repos.py confere no Postgres real."""

    def test_leituras_recentes_ate_o_cursor_as_mais_recentes_em_ordem_de_id(self, borda, analise):
        borda.gravar_leituras([leitura(seg=i * 10) for i in range(5)])
        ids = [l["id"] for l in analise.ler_leituras_novas(0)]
        recentes = analise.ler_leituras_recentes(ids[3], T0 + timedelta(seconds=10), 100)
        assert [l["payload"]["x"] for l in recentes] == [10, 20, 30]
        assert [l["payload"]["x"] for l in analise.ler_leituras_recentes(ids[3], T0, 2)] == [20, 30]
        assert analise.ler_leituras_recentes(0, T0, 100) == []

    def test_episodios_recentes_trazem_os_abertos_de_qualquer_idade(self, analise):
        base = {"user_id": 1, "device_id": None, "tipo": "X", "dados": {}}
        antigo = T0 - timedelta(days=2)
        analise.gravar_episodio({**base, "episodio_id": "velho-aberto", "estado": "aguardando_confirmacao",
                                 "aberto_em": antigo, "atualizado_em": antigo})
        analise.gravar_episodio({**base, "episodio_id": "velho-encerrado", "estado": "encerrado", "aberto_em": antigo,
                                 "atualizado_em": antigo, "encerrado_em": antigo, "motivo_encerramento": "EXPIRADO"})
        analise.gravar_episodio({**base, "episodio_id": "recente-encerrado", "estado": "encerrado", "aberto_em": T0,
                                 "atualizado_em": T0, "encerrado_em": T0, "motivo_encerramento": "CONFIRMADO"})
        achados = analise.listar_episodios_recentes(T0 - timedelta(hours=1))
        assert [e["episodio_id"] for e in achados] == ["velho-aberto", "recente-encerrado"]

    def test_comandos_de_episodios_trazem_o_message_id(self, analise):
        analise.gravar_comando(comando("m1", 0, episodio_id="e1"))
        analise.gravar_comando(comando("m2", 1, episodio_id="outro"))
        achados = analise.listar_comandos_de_episodios(["e1"])
        assert [(c["episodio_id"], c["message_id"]) for c in achados] == [("e1", "m1")]
        assert analise.listar_comandos_de_episodios([]) == []
