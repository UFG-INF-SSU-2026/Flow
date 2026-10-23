"""
S6: saude do pipeline (`borda/saude.py`) e o /health da borda. Sem Docker: banco falso e relogio explicito.

Cobre a classificacao pelos limiares, o monitor (cache, alarmes em log, thread) e a rota, que continua respondendo 200 com
`status: ok` e so acrescenta `pipeline` e `borda`.
"""
from datetime import timedelta

import pytest

from _apoio import aguardar
from _mundo import Mundo, queda_wokwi
from analise import const as const_analise
from borda import const as const_borda
from borda import saude, server, tempo
from comum import const as const_comum
from flow_db import repositorios


def linha(**extra):
    return {"atraso_ids": 0, "idade_batimento_analise_s": 0.2, "idade_ultimo_comando_entregue_s": None, **extra}


class TestClassificar:
    def test_saudavel(self):
        r = saude.classificar(linha())
        assert r["estado"] == saude.OK and r["motivos"] == []

    def test_batimento_velho_e_analise_parada(self):
        limiar = const_borda.LIMIAR_BATIMENTO_ANALISE_S
        assert saude.classificar(linha(idade_batimento_analise_s=limiar))["estado"] == saude.OK      # no limiar ainda vale
        r = saude.classificar(linha(idade_batimento_analise_s=limiar + 0.1))
        assert (r["estado"], r["motivos"]) == (saude.DEGRADADO, [saude.MOTIVO_ANALISE_PARADA])

    def test_atraso_acima_do_limiar(self):
        r = saude.classificar(linha(atraso_ids=const_borda.LIMIAR_ATRASO_LEITURAS + 1))
        assert (r["estado"], r["motivos"]) == (saude.DEGRADADO, [saude.MOTIVO_ATRASO])

    def test_os_dois_motivos_juntos(self):
        r = saude.classificar(linha(atraso_ids=10_000, idade_batimento_analise_s=60))
        assert r["motivos"] == [saude.MOTIVO_ANALISE_PARADA, saude.MOTIVO_ATRASO]

    def test_sem_batimento_algum(self):
        r = saude.classificar(linha(idade_batimento_analise_s=None))
        assert (r["estado"], r["motivos"]) == (saude.DEGRADADO, [saude.MOTIVO_SEM_BATIMENTO])

    def test_sem_como_ler_e_desconhecido_e_nao_ok(self):
        r = saude.classificar(None)
        assert (r["estado"], r["motivos"]) == (saude.DESCONHECIDO, [saude.MOTIVO_BANCO])

    def test_comando_antigo_nao_degrada(self):
        """Sem queda nao ha comando: a idade do ultimo comando entregue e so informativa."""
        assert saude.classificar(linha(idade_ultimo_comando_entregue_s=86_400))["estado"] == saude.OK

    def test_limiares_sao_lidos_na_hora_do_uso(self, monkeypatch):
        monkeypatch.setattr(const_borda, "LIMIAR_BATIMENTO_ANALISE_S", 1.0)
        assert saude.classificar(linha(idade_batimento_analise_s=1.5))["estado"] == saude.DEGRADADO

    def test_valores_arredondados(self):
        r = saude.classificar(linha(idade_batimento_analise_s=0.123456))
        assert r["idade_batimento_analise_s"] == 0.123


class RepoSaude:
    def __init__(self, **extra):
        self.dado, self.consultas, self.erro = linha(**extra), 0, None

    def ler_saude(self):
        self.consultas += 1
        if self.erro:
            raise self.erro
        return dict(self.dado)


class TestMonitor:
    @pytest.fixture
    def relogio(self):
        class R:
            t = 1000.0

            def __call__(self):
                return self.t
        return R()

    def monitor(self, repo, relogio, monitorada=True, estatisticas=dict):
        return saude.MonitorSaude(obter_repo=lambda: repo, monitorada=lambda: monitorada, estatisticas=estatisticas,
                                  relogio=relogio)

    def test_cache_curto_e_forcar_ignora_o_cache(self, relogio):
        repo = RepoSaude()
        mon = self.monitor(repo, relogio)
        mon.pipeline(), mon.pipeline()
        assert repo.consultas == 1
        mon.pipeline(forcar=True)
        assert repo.consultas == 2
        relogio.t += const_borda.CACHE_SAUDE_S + 0.1
        mon.pipeline()
        assert repo.consultas == 3

    def test_repositorio_novo_invalida_o_cache(self, relogio):
        repos = [RepoSaude(), RepoSaude()]
        atual = {"i": 0}
        mon = saude.MonitorSaude(obter_repo=lambda: repos[atual["i"]], relogio=relogio)
        mon.pipeline()
        atual["i"] = 1
        mon.pipeline()
        assert [r.consultas for r in repos] == [1, 1]

    def test_nao_monitorado_nao_consulta_o_banco(self, relogio):
        repo = RepoSaude()
        r = self.monitor(repo, relogio, monitorada=False).pipeline()
        assert r["estado"] == saude.NAO_MONITORADO and repo.consultas == 0

    def test_banco_fora_do_ar_vira_desconhecido_sem_levantar(self, relogio):
        repo = RepoSaude()
        repo.erro = ConnectionError("banco fora")
        assert self.monitor(repo, relogio).pipeline()["estado"] == saude.DESCONHECIDO

    def test_sem_repositorio_e_desconhecido(self, relogio):
        mon = saude.MonitorSaude(obter_repo=lambda: None, relogio=relogio)
        assert mon.pipeline()["estado"] == saude.DESCONHECIDO

    def test_corpo_traz_pipeline_e_contadores_da_borda(self, relogio):
        mon = self.monitor(RepoSaude(), relogio, estatisticas=lambda: {"fila_leituras": 3, "leituras_descartadas": 0})
        corpo = mon.corpo()
        assert corpo["pipeline"]["estado"] == saude.OK and corpo["borda"]["fila_leituras"] == 3

    def test_alarme_na_entrada_nao_repete_e_avisa_a_volta(self, relogio, capsys):
        repo = RepoSaude(idade_batimento_analise_s=60)
        mon = self.monitor(repo, relogio)
        mon.verificar()
        assert "ALARME pipeline degradado" in capsys.readouterr().out
        relogio.t += 5
        mon.verificar()
        assert "ALARME" not in capsys.readouterr().out                    # mesmo motivo, dentro do intervalo
        relogio.t += const_borda.INTERVALO_REPETIR_ALARME_S
        mon.verificar()
        assert "ALARME pipeline degradado" in capsys.readouterr().out     # repete enquanto durar
        repo.dado["idade_batimento_analise_s"] = 0.2
        mon.verificar()
        assert "pipeline normalizado" in capsys.readouterr().out
        mon.verificar()
        assert capsys.readouterr().out == ""

    def test_mudanca_de_motivo_alarma_na_hora(self, relogio, capsys):
        repo = RepoSaude(idade_batimento_analise_s=60)
        mon = self.monitor(repo, relogio)
        mon.verificar()
        capsys.readouterr()
        repo.dado["atraso_ids"] = 10_000
        mon.verificar()
        assert saude.MOTIVO_ATRASO in capsys.readouterr().out

    def test_alarme_de_banco_desconhecido_cita_o_tipo_do_erro(self, relogio, capsys):
        repo = RepoSaude()
        repo.erro = ConnectionError("senha=segredo")
        self.monitor(repo, relogio).verificar()
        saida = capsys.readouterr().out
        assert "ALARME pipeline desconhecido" in saida and "ConnectionError" in saida and "segredo" not in saida

    def test_alarme_de_leituras_descartadas_na_borda(self, relogio, capsys):
        contagem = {"n": 0}
        mon = self.monitor(RepoSaude(), relogio, estatisticas=lambda: {"leituras_descartadas": contagem["n"]})
        mon.verificar()
        assert "descartadas" not in capsys.readouterr().out
        contagem["n"] = 7
        mon.verificar()
        assert "+7" in capsys.readouterr().out
        mon.verificar()
        assert capsys.readouterr().out == ""

    def test_thread_monitora_e_para(self, monkeypatch):
        monkeypatch.setattr(const_borda, "INTERVALO_MONITOR_SAUDE_S", 0.01)
        repo = RepoSaude(idade_batimento_analise_s=60)
        mon = saude.MonitorSaude(obter_repo=lambda: repo)
        t = mon.iniciar()
        try:
            assert t.name == "flow-saude" and t.daemon and mon.iniciar() is t
            assert aguardar(lambda: mon._degradado_desde is not None)
        finally:
            mon.parar()
        assert not t.is_alive()


class TestRotaHealth:
    @pytest.fixture(autouse=True)
    def _monitor_novo(self, monkeypatch):
        monkeypatch.setattr(server, "_monitor", None)
        monkeypatch.setattr(const_borda, "CACHE_SAUDE_S", 0)

    def health(self, cli):
        r = cli.get("/health")
        assert r.status_code == 200
        return r.get_json()

    def test_processo_unico_no_caminho_direto_nao_monitora(self, cli):
        corpo = self.health(cli)
        assert corpo["status"] == "ok" and corpo["pipeline"]["estado"] == saude.NAO_MONITORADO
        assert corpo["borda"] == {"fila_leituras": 0, "leituras_descartadas": 0}

    @pytest.fixture
    def monitorada(self, monkeypatch):
        monkeypatch.setattr(const_comum, "ACOES_PELO_BANCO", True)
        monkeypatch.setattr(const_analise, "LACO_ATIVO", True)

    def test_acoes_pelo_banco_monitora_o_batimento(self, cli, monitorada, banco):
        assert self.health(cli)["pipeline"]["estado"] == saude.OK

    def test_borda_isolada_monitora(self, cli, monkeypatch, banco):
        monkeypatch.setattr(const_borda, "ISOLADA", True)
        assert self.health(cli)["pipeline"]["estado"] == saude.OK

    def test_acusa_a_analise_parada_logo_acima_do_limiar(self, cli, monitorada, banco):
        agora = banco.relogio()
        limiar = const_borda.LIMIAR_BATIMENTO_ANALISE_S
        banco.cursor_analise["atualizado_em"] = agora - timedelta(seconds=limiar - 1)
        assert self.health(cli)["pipeline"]["estado"] == saude.OK
        banco.cursor_analise["atualizado_em"] = agora - timedelta(seconds=limiar + 1)
        corpo = self.health(cli)
        assert corpo["status"] == "ok"                                     # a borda esta de pe; o pipeline nao
        assert (corpo["pipeline"]["estado"], corpo["pipeline"]["motivos"]) == (saude.DEGRADADO, [saude.MOTIVO_ANALISE_PARADA])

    def test_acusa_o_atraso_em_leituras(self, cli, monitorada, banco):
        banco.borda().gravar_leituras([
            {"instante_ingestao": tempo.agora_utc(), "user_id": 1, "device_id": "d", "fonte": "app", "tipo_evento": None,
             "schema_version": 3, "instante_origem": None, "conta_id": None, "payload": {}}
            for _ in range(const_borda.LIMIAR_ATRASO_LEITURAS + 1)])
        pipeline = self.health(cli)["pipeline"]
        assert pipeline["motivos"] == [saude.MOTIVO_ATRASO] and pipeline["atraso_leituras"] == const_borda.LIMIAR_ATRASO_LEITURAS + 1

    def test_banco_fora_do_ar_continua_200_e_diz_que_nao_sabe(self, cli, monitorada, banco, monkeypatch):
        def quebra():
            raise ConnectionError("banco fora")
        monkeypatch.setattr(repositorios.borda(), "ler_saude", quebra)
        assert self.health(cli)["pipeline"]["estado"] == saude.DESCONHECIDO

    def test_fila_de_leituras_aparece_no_corpo(self, cli, banco):
        server._enfileirar_leitura(1, "app", "cel1", {"x": 1}, 3, "idoso1", tempo.agora_utc())
        assert self.health(cli)["borda"]["fila_leituras"] == 1

    def test_o_corpo_nao_vaza_dado_de_usuario(self, cli, monitorada, banco):
        banco.borda().gravar_leituras(
            [{"instante_ingestao": tempo.agora_utc(), "user_id": 77, "device_id": "dispositivo-secreto", "fonte": "app",
              "tipo_evento": None, "schema_version": 3, "instante_origem": None, "conta_id": "conta-secreta",
              "payload": {"x": "valor-secreto"}}])
        texto = cli.get("/health").get_data(as_text=True)
        assert not any(x in texto for x in ("dispositivo-secreto", "conta-secreta", "valor-secreto"))

    def test_ponta_a_ponta_com_a_analise(self, cli, monitorada):
        """No banco falso com relogio explicito: a analise parada acusa, um ciclo dela renova o batimento e volta a ok."""
        m = Mundo()
        repositorios._borda.repo = m.borda      # a borda do servidor passa a enxergar o banco do mundo (o conftest restaura)
        m.borda.gravar_leituras(queda_wokwi())
        m.rel.ir_para(60)
        assert self.health(cli)["pipeline"]["estado"] == saude.DEGRADADO
        m.laco.executar_ciclo()
        assert self.health(cli)["pipeline"]["estado"] == saude.OK
