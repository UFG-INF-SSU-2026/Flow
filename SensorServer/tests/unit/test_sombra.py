"""
Comparador da S3b (analise/sombra.py), a coleta, o relatorio e as ferramentas (simular_cenarios, relatorio_sombra).
Sem Docker, sem rede, sem threads: registros montados a mao e relogio explicito. A prova ponta a ponta (os cenarios do
roadmap rodando nos dois caminhos) esta em tests/integration/test_sombra_cenarios.py.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import relatorio_sombra
import simular_cenarios as sc
from analise import const as const_analise
from analise import nucleo, regra_queda, sombra
from comum import mensagens

T0 = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)
D, S = sombra.LADO_DIRETO, sombra.LADO_SOMBRA


def em(t: float) -> datetime:
    return T0 + timedelta(seconds=t)


def reg(lado, tipo, chave, t=0.0, origem="leitura", visto=None, rec=False, user=1) -> sombra.Registro:
    return sombra.Registro(lado, origem, tipo, user, json.dumps(chave, sort_keys=True), None if t is None else em(t),
                           visto, rec)


def par(tipo, chave, t_d=0.0, t_s=0.0, **kw):
    return [reg(D, tipo, chave, t_d, **kw), reg(S, tipo, chave, t_s, **kw)]


def divergencias(registros, **kw):
    return sombra.comparar(registros, **kw).divergencias


# ============================================================================ registros_de
class TestRegistrosDe:
    def _comando(self, episodio_id, t=0.0, device="dev"):
        pedido = regra_queda.PedidoConfirmacao(episodio_id, 5, const_analise.MOTIVO_A)
        return nucleo.comando_de_pedido(nucleo.novo_estado(), 1, pedido, device, em(t))

    def test_a_chave_nao_depende_dos_ids_sorteados(self):
        a = sombra.registros_de(nucleo.Decisoes(comandos=[self._comando("ep-A")]), D, "leitura")
        b = sombra.registros_de(nucleo.Decisoes(comandos=[self._comando("ep-B")]), S, "leitura")
        assert [(r.tipo, r.chave) for r in a] == [(r.tipo, r.chave) for r in b]
        assert {r.tipo for r in a} == {sombra.T_COMANDO, sombra.T_EPISODIO}
        assert "ep-A" not in a[0].chave + a[1].chave

    def test_a_chave_distingue_o_que_a_decisao_diz(self):
        a = sombra.registros_de(nucleo.Decisoes(comandos=[self._comando("ep", device="x")]), D, "leitura")
        b = sombra.registros_de(nucleo.Decisoes(comandos=[self._comando("ep", device="y")]), S, "leitura")
        assert a[0].chave != b[0].chave

    def test_o_instante_do_registro_e_o_da_decisao(self):
        (cmd, epi) = sombra.registros_de(nucleo.Decisoes(comandos=[self._comando("ep", t=7)]), D, "leitura")
        assert cmd.instante == epi.instante == em(7)

    def test_descarte_nao_tem_instante(self):
        (r,) = sombra.registros_de(nucleo.Decisoes(descartes=[nucleo.Descarte(1, "d", nucleo.DESCARTE_DUPLICADA, "wokwi")]),
                                   D, "leitura")
        assert r.tipo == sombra.T_DESCARTE and r.instante is None

    def test_confirmacao_ignora_ids_e_ordem_das_fontes(self):
        def conf(fontes, ep):
            return nucleo.Decisoes(confirmacoes=[nucleo.Confirmacao(1, "episodio.aberto", {
                "episodeId": ep, "motivo": "M", "fontes_usadas": fontes}, em(0))])
        f1 = [{"fonte": "app", "device_id": "c", "versao": 3, "degradada": False},
              {"fonte": "wokwi", "device_id": "b", "versao": 3, "degradada": False}]
        a = sombra.registros_de(conf(f1, "x"), D, "leitura")[0]
        b = sombra.registros_de(conf(list(reversed(f1)), "y"), S, "leitura")[0]
        assert a.chave == b.chave

    def test_resposta_de_mensagem_desconhecida_difere_da_conhecida(self):
        def conf(ep):
            return nucleo.Decisoes(confirmacoes=[nucleo.Confirmacao(1, "push.ack", {"episodeId": ep}, em(0))])
        assert (sombra.registros_de(conf("ep"), D, "resposta")[0].chave
                != sombra.registros_de(conf(None), S, "resposta")[0].chave)

    def test_json_ida_e_volta(self):
        r = reg(D, sombra.T_ALERTA, {"a": 1}, t=3, visto=12.5)
        assert sombra.Registro.de_json(json.loads(json.dumps(r.como_json()))) == r
        sem_instante = reg(S, sombra.T_DESCARTE, {"a": 1}, t=None, rec=True)
        assert sombra.Registro.de_json(sem_instante.como_json()) == sem_instante


# ============================================================================ comparar
class TestComparar:
    def test_iguais_nao_divergem(self):
        res = sombra.comparar(par(sombra.T_ALERTA, {"a": 1}) + par(sombra.T_COMANDO, {"m": "x"}, t_d=2, t_s=2))
        assert res.completa and res.contagem[sombra.T_ALERTA] == {"direto": 1, "sombra": 1, "pareados": 1}

    def test_decisao_faltando_na_sombra(self):
        (d,) = divergencias([reg(D, sombra.T_ALERTA, {"a": 1})])
        assert d.tipo == sombra.D_FALTANDO and d.direto is not None and d.sombra is None

    def test_decisao_a_mais_na_sombra(self):
        (d,) = divergencias([reg(S, sombra.T_ALERTA, {"a": 1})])
        assert d.tipo == sombra.D_A_MAIS and d.sombra is not None and d.direto is None

    def test_chave_diferente_vira_uma_falta_e_uma_sobra(self):
        tipos = sorted(d.tipo for d in divergencias([reg(D, sombra.T_ALERTA, {"a": 1}), reg(S, sombra.T_ALERTA, {"a": 2})]))
        assert tipos == [sombra.D_A_MAIS, sombra.D_FALTANDO]

    def test_o_mesmo_tipo_de_decisao_em_outro_usuario_nao_se_pareia(self):
        tipos = sorted(d.tipo for d in divergencias([reg(D, sombra.T_ALERTA, {"a": 1}, user=1),
                                                     reg(S, sombra.T_ALERTA, {"a": 1}, user=2)]))
        assert tipos == [sombra.D_A_MAIS, sombra.D_FALTANDO]

    def test_pareia_na_ordem_e_acusa_so_a_sobra(self):
        regs = par(sombra.T_ALERTA, {"a": 1}, 0, 0) + [reg(D, sombra.T_ALERTA, {"a": 1}, 5)]
        (d,) = divergencias(regs)
        assert d.tipo == sombra.D_FALTANDO and d.direto.instante == em(5)

    def test_instante_dentro_da_tolerancia(self):
        folga = const_analise.ORCAMENTO_LATENCIA_S
        assert not divergencias(par(sombra.T_ALERTA, {"a": 1}, 0, folga))

    def test_instante_fora_da_tolerancia(self):
        (d,) = divergencias(par(sombra.T_ALERTA, {"a": 1}, 0, const_analise.ORCAMENTO_LATENCIA_S + 0.1))
        assert d.tipo == sombra.D_INSTANTE

    def test_vigia_tem_tolerancia_maior(self):
        dif = const_analise.INTERVALO_VIGIA_S + const_analise.ORCAMENTO_LATENCIA_S
        assert not divergencias(par(sombra.T_COMANDO, {"a": 1}, 0, dif, origem="vigia"))
        assert divergencias(par(sombra.T_COMANDO, {"a": 1}, 0, dif + 0.1, origem="vigia"))
        assert sombra.tolerancia_instante("vigia", "leitura") == dif
        assert sombra.tolerancia_instante("leitura", "resposta") == const_analise.ORCAMENTO_LATENCIA_S

    def test_origem_diferente(self):
        regs = [reg(D, sombra.T_COMANDO, {"a": 1}, origem="resposta"), reg(S, sombra.T_COMANDO, {"a": 1}, origem="vigia")]
        assert [d.tipo for d in divergencias(regs)] == [sombra.D_ORIGEM]

    def test_descarte_compara_sem_instante(self):
        assert not divergencias(par(sombra.T_DESCARTE, {"motivo": "duplicada"}, t_d=None, t_s=None))

    def test_reconstrucao_da_sombra_fica_fora_e_e_contada(self):
        res = sombra.comparar([reg(S, sombra.T_ALERTA, {"a": 1}, rec=True)] + par(sombra.T_COMANDO, {"m": 1}))
        assert res.completa and res.reconstrucoes == 1 and res.contagem[sombra.T_ALERTA]["sombra"] == 0

    def test_estado_so_conta_quando_a_assinatura_muda(self):
        e1, e2 = {"device_id": "d", "estado_atual": "NORMAL"}, {"device_id": "d", "estado_atual": "ALERTA"}
        direto = [reg(D, sombra.T_ESTADO, e1, 0), reg(D, sombra.T_ESTADO, e1, 1), reg(D, sombra.T_ESTADO, e2, 2)]
        sombra_ = [reg(S, sombra.T_ESTADO, e1, 0), reg(S, sombra.T_ESTADO, e2, 2), reg(S, sombra.T_ESTADO, e2, 3)]
        res = sombra.comparar(direto + sombra_)
        assert res.completa and res.contagem[sombra.T_ESTADO]["direto"] == 2

    def test_estado_repetido_so_e_colapsado_por_dispositivo(self):
        a, b = {"device_id": "a", "estado_atual": "N"}, {"device_id": "b", "estado_atual": "N"}
        regs = [reg(D, sombra.T_ESTADO, a, 0), reg(D, sombra.T_ESTADO, b, 1), reg(D, sombra.T_ESTADO, a, 2)]
        assert sombra.comparar(regs).contagem[sombra.T_ESTADO]["direto"] == 2      # a, b (o segundo `a` e repeticao)

    def test_pendente_enquanto_a_sombra_ainda_pode_chegar(self):
        recente = reg(D, sombra.T_ALERTA, {"a": 1}, visto=100.0)
        res = sombra.comparar([recente], agora=100.2)
        assert res.pendentes == 1 and not res.divergencias and not res.completa
        res = sombra.comparar([recente], agora=100.0 + 10)
        assert res.pendentes == 0 and [d.tipo for d in res.divergencias] == [sombra.D_FALTANDO]

    def test_sem_agora_tudo_e_definitivo(self):
        assert sombra.comparar([reg(D, sombra.T_ALERTA, {"a": 1}, visto=100.0)]).pendentes == 0

    def test_atraso_so_conta_pares_de_leitura_e_resposta(self):
        regs = (par(sombra.T_ALERTA, {"a": 1}, visto=None)
                + [reg(D, sombra.T_COMANDO, {"v": 1}, origem="vigia", visto=1.0),
                   reg(S, sombra.T_COMANDO, {"v": 1}, origem="vigia", visto=9.0),
                   reg(D, sombra.T_ALERTA, {"b": 1}, visto=10.0), reg(S, sombra.T_ALERTA, {"b": 1}, visto=10.25)])
        res = sombra.comparar(regs)
        assert res.atrasos_s == [0.25] and res.completa

    def test_estatisticas_do_atraso_e_o_orcamento(self):
        res = sombra.Comparacao(atrasos_s=[0.1, 0.2, 0.3, 0.4])
        a = res.atraso()
        assert (a["n"], a["max"]) == (4, 0.4) and a["mediana"] == pytest.approx(0.25) and a["p95"] == 0.4
        assert res.atraso_dentro_do_orcamento
        assert not sombra.Comparacao(atrasos_s=[0.1, const_analise.ORCAMENTO_LATENCIA_S + 0.5]).atraso_dentro_do_orcamento
        assert sombra.Comparacao().atraso() == {"n": 0, "mediana": None, "p95": None, "max": None}

    def test_filtrar_janela(self):
        regs = [reg(D, sombra.T_ALERTA, {"a": 1}, visto=v) for v in (1.0, 5.0, 9.0)] + [reg(D, sombra.T_ALERTA, {"a": 1})]
        assert [r.visto_em for r in sombra.filtrar_janela(regs, 2.0, 9.0)] == [5.0, 9.0]


# ============================================================================ coleta (Sombra)
class TestColeta:
    def _decisoes(self, ep="ep", t=0.0, tipo_comando=True):
        if tipo_comando:
            pedido = regra_queda.PedidoConfirmacao(ep, 5, const_analise.MOTIVO_A)
            cmd = nucleo.comando_de_pedido(nucleo.novo_estado(), 1, pedido, "dev", em(t))
            return nucleo.Decisoes(comandos=[cmd])
        return nucleo.Decisoes(alertas=[nucleo.Alerta(1, "d", "imobilidade", {"mensagem": "m"}, 3, em(t))])

    def test_registra_com_o_relogio_injetado_e_devolve_o_que_registrou(self):
        c = sombra.Sombra(relogio=lambda: 42.0)
        novos = c.registrar(D, self._decisoes(), "leitura")
        assert len(novos) == 2 and {r.visto_em for r in c.registros()} == {42.0}

    def test_decisao_vazia_nao_registra(self):
        c = sombra.Sombra()
        assert c.registrar(D, nucleo.Decisoes(), "leitura") == [] and c.registros() == []

    def test_teto_de_memoria_descarta_os_mais_antigos(self):
        c = sombra.Sombra(max_registros=3)
        for k in range(5):
            c.registrar(D, self._decisoes(tipo_comando=False, t=k), "leitura")
        assert [r.instante for r in c.registros()] == [em(2), em(3), em(4)]

    def test_nunca_levanta_no_caminho_dos_dados(self):
        assert sombra.Sombra().registrar(D, object(), "leitura") == []

    def test_grava_jsonl_e_le_de_volta(self, tmp_path):
        caminho = str(tmp_path / "pasta" / "sombra.jsonl")
        c = sombra.Sombra(caminho)
        c.registrar(D, self._decisoes(tipo_comando=False), "leitura")
        c.registrar(S, self._decisoes(tipo_comando=False), "leitura")
        assert sombra.ler_jsonl(caminho) == c.registros()

    def test_ler_pula_linha_ilegivel_e_arquivo_ausente(self, tmp_path):
        caminho = tmp_path / "s.jsonl"
        ok = reg(D, sombra.T_ALERTA, {"a": 1}, visto=1.0)
        caminho.write_text(json.dumps(ok.como_json()) + "\n{corta no meio\n\n[1, 2]\n", encoding="utf-8")
        assert sombra.ler_jsonl(str(caminho)) == [ok]
        assert sombra.ler_jsonl(str(tmp_path / "nao-existe.jsonl")) == []

    def test_falha_de_gravacao_nao_derruba_a_coleta(self, tmp_path, capsys):
        arquivo = tmp_path / "e_um_arquivo"
        arquivo.write_text("x")
        c = sombra.Sombra(str(arquivo / "filho" / "s.jsonl"))       # o "diretorio" pai e um arquivo
        assert len(c.registrar(D, self._decisoes(tipo_comando=False), "leitura")) == 1
        assert len(c.registros()) == 1 and "falha ao gravar" in capsys.readouterr().out

    def test_limpar(self):
        c = sombra.Sombra()
        c.registrar(D, self._decisoes(), "leitura")
        c.limpar()
        assert c.registros() == []


class TestTraducaoDeIds:
    def _pedido(self, c, lado, ep, t=0.0):
        pedido = regra_queda.PedidoConfirmacao(ep, 5, const_analise.MOTIVO_A)
        cmd = nucleo.comando_de_pedido(nucleo.novo_estado(), 1, pedido, "dev", em(t))
        c.registrar(lado, nucleo.Decisoes(comandos=[cmd]), "leitura")
        return cmd.mensagem["messageId"]

    def _resposta(self, mid, user=1):
        return nucleo.Resposta(em(1), user, "d", "app", {"eventType": "confirmacao.resposta", "state": "ESTOU_BEM",
                                                         "messageId": mid}, 3)

    def test_troca_o_id_do_direto_pelo_do_pedido_equivalente_da_sombra(self):
        c = sombra.Sombra()
        m_dir, m_som = self._pedido(c, D, "a"), self._pedido(c, S, "b")
        assert m_dir != m_som
        r = c.traduzir_resposta(self._resposta(m_dir))
        assert r.evento["messageId"] == m_som and r.evento["state"] == "ESTOU_BEM" and r.user_id == 1

    def test_o_i_esimo_do_direto_corresponde_ao_i_esimo_da_sombra(self):
        c = sombra.Sombra()
        d1, d2 = self._pedido(c, D, "a1", 0), self._pedido(c, D, "a2", 100)
        s1, s2 = self._pedido(c, S, "b1", 0), self._pedido(c, S, "b2", 100)
        assert c.traduzir_resposta(self._resposta(d2)).evento["messageId"] == s2
        assert c.traduzir_resposta(self._resposta(d1)).evento["messageId"] == s1

    def test_id_que_o_direto_nao_emitiu_segue_como_esta(self):
        c = sombra.Sombra()
        self._pedido(c, D, "a")
        r = self._resposta("qualquer-coisa")
        assert c.traduzir_resposta(r) is r

    def test_id_de_outro_usuario_nao_e_traduzido(self):
        c = sombra.Sombra()
        m_dir, _ = self._pedido(c, D, "a"), self._pedido(c, S, "b")
        r = self._resposta(m_dir, user=2)
        assert c.traduzir_resposta(r).evento["messageId"] == m_dir

    def test_sombra_ainda_sem_pedido_nao_reconhece_a_resposta(self):
        c = sombra.Sombra()
        m_dir = self._pedido(c, D, "a")
        assert c.traduzir_resposta(self._resposta(m_dir)).evento["messageId"] != m_dir

    def test_reconstrucao_da_sombra_nao_entra_na_correspondencia(self):
        c = sombra.Sombra()
        pedido = regra_queda.PedidoConfirmacao("velho", 5, const_analise.MOTIVO_A)
        velho = nucleo.comando_de_pedido(nucleo.novo_estado(), 1, pedido, "dev", em(-100))
        c.registrar(S, nucleo.Decisoes(comandos=[velho]), "leitura", reconstrucao=True)
        m_dir, m_som = self._pedido(c, D, "a"), self._pedido(c, S, "b")
        assert c.traduzir_resposta(self._resposta(m_dir)).evento["messageId"] == m_som

    def test_so_pedidos_entram_nao_encerramentos(self):
        c = sombra.Sombra()
        enc = nucleo.comando_de_encerramento(regra_queda.Encerramento(1, "ep", "EXPIRADO"), em(0))
        c.registrar(D, nucleo.Decisoes(comandos=[enc]), "vigia")
        assert enc.mensagem["messageType"] == mensagens.TIPO_ENCERRADA
        r = self._resposta(enc.mensagem["messageId"])
        assert c.traduzir_resposta(r) is r


# ============================================================================ relatorio
class TestRelatorio:
    def _ok(self, nome="cenario_ok"):
        regs = [reg(D, sombra.T_ALERTA, {"a": 1}, visto=1.0), reg(S, sombra.T_ALERTA, {"a": 1}, visto=1.25)]
        return sombra.ResultadoCenario(nome, "descricao do cenario", sombra.comparar(regs))

    def test_markdown_com_resumo_criterios_e_divergencias(self):
        ruim = sombra.ResultadoCenario("cenario_ruim", "", sombra.comparar([reg(D, sombra.T_ALERTA, {"a": 1})]))
        texto = sombra.renderizar_markdown([self._ok(), ruim], origem_dos_dados="dataUsers/x.jsonl")
        assert "# Relatório de comparação em modo sombra (S3b)" in texto and "dataUsers/x.jsonl" in texto
        assert "| cenario_ok | 1 | 1 | 1 | 0 | 0.250 | **OK** |" in texto
        assert "**DIVERGE**" in texto and sombra.D_FALTANDO in texto
        assert "- [ ] Em todos os cenários" in texto             # um divergiu: criterio nao cumprido

    def test_criterios_marcados_quando_tudo_ok(self):
        texto = sombra.renderizar_markdown([self._ok("a"), self._ok("b")])
        assert "- [x] Em todos os cenários" in texto and "- [x] Atraso típico" in texto
        assert "- [ ] Relatório arquivado" in texto             # o arquivamento e manual

    def test_veredito_pendente_e_atraso(self):
        pendente = sombra.ResultadoCenario("p", "", sombra.comparar([reg(D, sombra.T_ALERTA, {"a": 1}, visto=5.0)], agora=5.1))
        lento = sombra.ResultadoCenario("l", "", sombra.comparar(
            [reg(D, sombra.T_ALERTA, {"a": 1}, visto=1.0), reg(S, sombra.T_ALERTA, {"a": 1}, visto=3.0)]))
        texto = sombra.renderizar_markdown([pendente, lento])
        assert "**PENDENTE**" in texto and "**ATRASO**" in texto

    def test_sem_cenarios_nao_marca_criterio(self):
        assert "- [x]" not in sombra.renderizar_markdown([])

    def test_lista_de_divergencias_e_truncada(self):
        regs = [reg(D, sombra.T_ALERTA, {"n": k}) for k in range(25)]
        texto = sombra.renderizar_markdown([sombra.ResultadoCenario("muitas", "", sombra.comparar(regs))])
        assert "... e mais 5" in texto


class TestFerramentaDeRelatorio:
    def test_monta_um_resultado_por_cenario_do_manifesto(self):
        regs = (par(sombra.T_ALERTA, {"a": 1}, visto=10.0) + [reg(D, sombra.T_ALERTA, {"b": 1}, visto=50.0)])
        manifesto = {"cenarios": [{"nome": "um", "descricao": "d", "inicio": 9.0, "fim": 11.0},
                                  {"nome": "dois", "descricao": "", "inicio": 49.0, "fim": 51.0}]}
        um, dois = relatorio_sombra.montar(regs, manifesto, agora=1000.0)
        assert (um.nome, um.comparacao.completa) == ("um", True)
        assert (dois.nome, [d.tipo for d in dois.comparacao.divergencias]) == ("dois", [sombra.D_FALTANDO])

    def test_sem_manifesto_compara_tudo_junto(self):
        (r,) = relatorio_sombra.montar(par(sombra.T_ALERTA, {"a": 1}), None, agora=1000.0)
        assert "uso real" in r.nome and r.comparacao.completa

    def test_main_grava_o_relatorio_e_o_codigo_de_saida_reflete_o_veredito(self, tmp_path, capsys):
        arquivo, saida = tmp_path / "s.jsonl", tmp_path / "docs" / "rel.md"
        regs = [reg(D, sombra.T_ALERTA, {"a": 1}, visto=1.0), reg(S, sombra.T_ALERTA, {"a": 1}, visto=1.25)]
        arquivo.write_text("".join(json.dumps(r.como_json()) + "\n" for r in regs), encoding="utf-8")
        args = ["--arquivo", str(arquivo), "--manifesto", str(tmp_path / "nao.json"), "--saida", str(saida)]
        assert relatorio_sombra.main(args) == 0
        assert saida.read_text(encoding="utf-8").startswith("# Relatório de comparação")
        arquivo.write_text(json.dumps(regs[0].como_json()) + "\n", encoding="utf-8")
        assert relatorio_sombra.main(args) == 1
        assert "DIVERGE" in capsys.readouterr().out

    def test_main_sem_decisoes_avisa_e_sai_com_2(self, tmp_path, capsys):
        assert relatorio_sombra.main(["--arquivo", str(tmp_path / "vazio.jsonl"), "--saida", str(tmp_path / "r.md")]) == 2
        assert "FLOW_SOMBRA=1" in capsys.readouterr().out

    def test_argumento_desconhecido(self):
        with pytest.raises(SystemExit):
            relatorio_sombra.main(["--nada"])


# ============================================================================ cenarios
class TestCenarios:
    def test_os_cenarios_do_roadmap_existem_com_nomes_unicos(self):
        nomes = [c.nome for c in sc.cenarios()]
        assert len(nomes) == len(set(nomes))
        assert {"queda_estou_bem", "queda_sem_resposta", "imobilidade_gatilho_b", "luz_inadequada",
                "app_e_wokwi_juntos"} <= set(nomes)

    def test_cada_passo_chama_um_metodo_da_sessao_e_cabe_na_duracao(self):
        for c in sc.cenarios():
            assert c.passos and all(hasattr(sc.Sessao, p.acao) for p in c.passos), c.nome
            assert max(p.t for p in c.passos) < c.duracao_s, c.nome

    def test_so_os_cenarios_com_app_dizem_que_precisam_dele(self):
        usa_app = {c.nome for c in sc.cenarios() if any(p.acao.startswith("app_") or p.args.get("canal") == "app"
                                                        for p in c.passos)}
        assert usa_app == {c.nome for c in sc.cenarios() if c.precisa_app}

    def test_cenarios_que_abrem_episodio_dizem_que_abrem(self):
        for c in sc.cenarios():
            abre = any(p.acao in ("estou_bem", "ack") for p in c.passos)
            assert c.abre_episodio == abre, c.nome

    def test_os_prazos_vem_das_constantes(self, monkeypatch):
        antes = {c.nome: c.duracao_s for c in sc.cenarios()}
        monkeypatch.setattr(sc.const_comum, "TIMEOUT_CONFIRMACAO_S", sc.const_comum.TIMEOUT_CONFIRMACAO_S + 10)
        depois = {c.nome: c.duracao_s for c in sc.cenarios()}
        assert depois["queda_sem_resposta"] == antes["queda_sem_resposta"] + 10 and depois["queda_estou_bem"] == antes["queda_estou_bem"]


class _Transporte:
    def __init__(self, mid="m1"):
        self.enviados, self.esperas, self.mid = [], [], mid

    def postar(self, rota, fonte, corpo):
        self.enviados.append((rota, fonte, corpo))
        return 200

    def esperar_ate(self, t):
        self.esperas.append(t)

    def ultimo_message_id(self):
        return self.mid


class TestSessao:
    def _sessao(self, **kw):
        t = _Transporte(**kw)
        return t, sc.Sessao(t, 7, "bridge-x", "cel-x", seq_inicial=100)

    def test_wokwi_usa_sequence_crescente_e_o_usuario_e_o_dispositivo_informados(self):
        t, s = self._sessao()
        s.wokwi_leitura(1.0, 1.5)
        s.wokwi_decisao("ALERTA_IMOBILIDADE")
        (_, f1, a), (_, _, b) = t.enviados
        assert f1 == "wokwi" and b["sequence"] == a["sequence"] + 1
        assert (a["user_id"], a["deviceId"], a["source"], a["eventType"]) == (7, "bridge-x", "wokwi", "imobilidade.leitura")
        assert b["eventType"] == "imobilidade.decisao" and b["state"] == "ALERTA_IMOBILIDADE"

    def test_repetir_reenvia_o_mesmo_sequence(self):
        t, s = self._sessao()
        s.wokwi_repetir()                        # nada a repetir ainda
        s.wokwi_leitura(1.0, 1.5)
        s.wokwi_repetir()
        assert len(t.enviados) == 2 and t.enviados[0][2] == t.enviados[1][2]

    def test_app_telemetria_nunca_repete_timestamp(self):
        t, s = self._sessao()
        s.app_telemetria(9.8, 9.9)
        s.app_telemetria(9.8, 9.9)
        a, b = t.enviados[0][2], t.enviados[1][2]
        assert b["timestamp_envio"] > a["timestamp_envio"] and a["device_id"] == "cel-x" and a["user_id"] == 7

    def test_ambiente_novo_incrementa_seq_num_e_repetir_reenvia_o_mesmo_bloco(self):
        t, s = self._sessao()
        s.app_ambiente(cct=6500)
        s.app_ambiente(cct=6500)
        s.app_ambiente(cct=6500, repetir=True)
        b1, b2, b3 = (e[2]["leitura_ambiente"] for e in t.enviados)
        assert b2["seq_num"] == b1["seq_num"] + 1 and b3 == b2
        assert b1["event_time"].startswith("2026-10-14T22")           # a noite, senao a regra de luz nao vale

    def test_respostas_levam_o_message_id_do_ultimo_pedido(self):
        t, s = self._sessao(mid="pedido-9")
        s.ack()
        s.estou_bem(canal="app")
        (r1, f1, a), (r2, f2, b) = t.enviados
        assert (r1, f1, a["eventType"], a["messageId"], a["deviceId"]) == ("/eventos", "wokwi", "push.ack", "pedido-9", "bridge-x")
        assert (r2, f2, b["eventType"], b["state"], b["device_id"]) == ("/eventos", "app", "confirmacao.resposta",
                                                                      "ESTOU_BEM", "cel-x")

    def test_sem_pedido_nao_envia_resposta(self, capsys):
        t, s = self._sessao(mid=None)
        s.estou_bem()
        assert t.enviados == [] and "nenhum pedido" in capsys.readouterr().out

    def test_executar_roda_os_passos_em_ordem_de_tempo_e_espera_a_duracao(self):
        t, s = self._sessao()
        c = sc.Cenario("x", "", (sc.Passo(2, "wokwi_leitura", {"v": 1.0}), sc.Passo(1, "wokwi_leitura", {"v": 0.5})), 9)
        sc.executar(c, s)
        assert [e[2]["value"] for e in t.enviados] == [0.5, 1.0] and t.esperas == [1, 2, 9]

    def test_argumentos_da_linha_de_comando(self):
        assert sc._argumentos([]) == ("http://localhost:5000", None, False)
        assert sc._argumentos(["http://x:1", "--cenarios", "a, b", "--lista"]) == ("http://x:1", ["a", "b"], True)
