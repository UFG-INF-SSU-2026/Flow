"""S5 (dois processos): ambiente por servico, trava de isolamento, borda isolada sem a analise, desligamento ordenado."""
import ast
import glob
import os
import subprocess
import sys

import pytest

from _apoio import SEGREDO
from borda import server, via_banco
from borda import const as const_borda
from borda.sse_hub import HUB
from comum import isolamento
from flow_db import repositorios

RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # SensorServer/


def _rodar(args, tmp_path, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("FLOW_")}
    env.update({"FLOW_SEM_DOTENV": "1", "PYTHONPATH": RAIZ})   # sem FLOW_CONTAS_PATH: a analise o recusaria
    env.update(extra)
    return subprocess.run([sys.executable, *args], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)


# ------------------------------- ambiente por servico ------------------------
class TestAmbientePorServico:
    @pytest.mark.parametrize("servico,arquivo", [("", ".env"), ("borda", ".env.borda"), ("analise", ".env.analise")])
    def test_cada_servico_le_o_seu_arquivo(self, tmp_path, servico, arquivo):
        r = _rodar(["-c", "from comum import const; print(const.DOTENV_ARQUIVO.name)"], tmp_path, FLOW_SERVICO=servico)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == arquivo

    def test_servico_desconhecido_recusa(self, tmp_path):
        r = _rodar(["-c", "from comum import const"], tmp_path, FLOW_SERVICO="outro")
        assert r.returncode != 0 and "FLOW_SERVICO" in r.stderr


# ------------------------------- trava de isolamento -------------------------
class TestIsolamento:
    def test_ambiente_limpo_passa(self):
        isolamento.exigir_ambiente("borda", {"FLOW_JWT_SECRET": "x", "FLOW_DB_BORDA_PASSWORD": "y"})
        isolamento.exigir_ambiente("analise", {"FLOW_DB_ANALISE_PASSWORD": "y"})

    @pytest.mark.parametrize("nome", ["FLOW_JWT_SECRET", "FLOW_DB_BORDA_PASSWORD", "FLOW_DB_PASSWORD",
                                      "FLOW_CONTAS_PATH", "FLOW_SESSOES_PATH"])
    def test_a_analise_recusa_segredo_da_borda(self, nome):
        with pytest.raises(isolamento.AmbienteInvalido) as e:
            isolamento.exigir_ambiente("analise", {nome: "valor-secreto-123"})
        assert nome in str(e.value) and "valor-secreto-123" not in str(e.value)

    @pytest.mark.parametrize("nome", ["FLOW_DB_ANALISE_PASSWORD", "FLOW_DB_PASSWORD"])
    def test_a_borda_recusa_segredo_da_analise_e_a_senha_administrativa(self, nome):
        with pytest.raises(isolamento.AmbienteInvalido) as e:
            isolamento.exigir_ambiente("borda", {nome: "valor-secreto-123"})
        assert nome in str(e.value) and "valor-secreto-123" not in str(e.value)

    def test_variavel_vazia_nao_conta(self):
        assert isolamento.variaveis_proibidas("analise", {"FLOW_JWT_SECRET": ""}) == []

    def test_servico_desconhecido(self):
        with pytest.raises(ValueError):
            isolamento.exigir_ambiente("x", {})

    def test_o_processo_da_analise_se_recusa_a_subir_com_o_segredo_jwt(self, tmp_path):
        r = _rodar(["-m", "analise"], tmp_path, FLOW_JWT_SECRET=SEGREDO, FLOW_DB_ANALISE_PASSWORD="a" * 20)
        assert r.returncode == 1
        assert "FLOW_JWT_SECRET" in r.stderr and SEGREDO not in r.stderr

    def test_o_processo_da_borda_se_recusa_a_subir_com_a_senha_da_analise(self, tmp_path):
        pytest.importorskip("flask")
        r = _rodar(["-m", "borda"], tmp_path, FLOW_JWT_SECRET=SEGREDO, FLOW_DB_BORDA_PASSWORD="b" * 20,
                   FLOW_DB_ANALISE_PASSWORD="a" * 20)
        assert r.returncode == 1
        assert "FLOW_DB_ANALISE_PASSWORD" in r.stderr and "a" * 20 not in r.stderr

    def test_a_analise_sem_banco_recusa_sem_vazar_senha(self, tmp_path):
        r = _rodar(["-m", "analise"], tmp_path, FLOW_DB_PORT="1", FLOW_DB_ANALISE_PASSWORD="a" * 20)
        assert r.returncode == 1
        assert "banco" in r.stderr.lower() and "a" * 20 not in r.stderr

    def test_a_borda_isolada_sem_banco_recusa_sem_vazar_senha(self, tmp_path):
        pytest.importorskip("flask")
        r = _rodar(["-m", "borda"], tmp_path, FLOW_JWT_SECRET=SEGREDO, FLOW_DB_PORT="1",
                   FLOW_DB_BORDA_PASSWORD="b" * 20)
        assert r.returncode == 1
        assert "banco" in r.stderr.lower() and "b" * 20 not in r.stderr

    def test_o_banco_falso_nao_vale_com_processos_separados(self, tmp_path):
        r = _rodar(["-m", "analise"], tmp_path, FLOW_DB_FAKE="1", FLOW_DB_ANALISE_PASSWORD="a" * 20)
        assert r.returncode == 1 and "FLOW_DB_FAKE" in r.stderr

    def test_os_pontos_de_entrada_recusam_o_uso_direto(self):
        from analise import servico as servico_analise
        from borda import servico as servico_borda
        assert servico_borda.main() == 1 and servico_analise.main() == 1     # FLOW_SERVICO nao esta definido nos testes


# ------------------------------- a analise nao tem porta de rede -------------------------
REDE = {"socket", "socketserver", "http", "wsgiref", "flask", "werkzeug", "requests", "urllib", "ssl", "asyncio"}


@pytest.mark.parametrize("arquivo", sorted(glob.glob(os.path.join(RAIZ, "analise", "*.py"))))
def test_a_analise_nao_importa_nada_de_rede(arquivo):
    with open(arquivo, encoding="utf-8") as f:
        arvore = ast.parse(f.read())
    raizes = set()
    for n in ast.walk(arvore):
        if isinstance(n, ast.Import):
            raizes |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            raizes.add(n.module.split(".")[0])
    assert not raizes & REDE, f"{os.path.basename(arquivo)} importa {sorted(raizes & REDE)}"


def test_a_borda_isolada_nunca_carrega_a_analise(tmp_path):
    """Importa o servidor como borda isolada, usa o pipeline e confere que nenhum modulo `analise*` foi carregado."""
    pytest.importorskip("flask")
    sonda = ("import sys; from borda import server; p = server._pipeline(); "
             "print(p.__name__, sorted(m for m in sys.modules if m == 'analise' or m.startswith('analise.')))")
    r = _rodar(["-c", sonda], tmp_path, FLOW_SEM_THREADS="1", FLOW_BORDA_ISOLADA="1", FLOW_JWT_SECRET=SEGREDO)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "borda.via_banco []"


# ------------------------------- pipeline da borda isolada -------------------------
class TestViaBanco:
    def test_nao_decide_nada(self, banco):
        assert via_banco.analisar_leitura(1, "d", "app", {}, 3, None) is None
        assert via_banco.analisar_resposta(1, "d", "app", {}, 3, None) is None

    def test_estado_vem_do_banco(self, banco):
        assert via_banco.estado_do_dispositivo("bridge-dev") is None

    def test_o_servidor_escolhe_o_pipeline_pela_constante(self, monkeypatch):
        monkeypatch.setattr(const_borda, "ISOLADA", True)
        assert server._pipeline() is via_banco
        monkeypatch.setattr(const_borda, "ISOLADA", False)
        assert server._pipeline().__name__ == "borda.caminho_direto"

    def test_as_rotas_nao_decidem_na_borda_isolada(self, monkeypatch, cli, tokens, banco):
        monkeypatch.setattr(const_borda, "ISOLADA", True)
        corpo = {"schemaVersion": 3, "source": "wokwi", "user_id": 1, "eventType": "imobilidade.leitura",
                 "deviceId": "bridge-dev", "entityId": "x", "eventTimeMs": 1000, "sequence": 1, "value": 0.3,
                 "valueMax": 0.4, "unit": "m/s2", "state": "NORMAL"}
        assert cli.post("/dados", corpo, tokens["dispositivo"]).status_code == 200
        server._descarregar_leituras()
        assert len(banco.tabelas["leituras"]) == 1                       # a borda gravou...
        assert banco.tabelas["comandos_push"] == [] and banco.tabelas["alertas"] == []   # ...e nao decidiu

    def test_entrega_sobe_e_para(self, monkeypatch):
        chamadas = []

        class Falsa:
            def iniciar(self):
                chamadas.append("iniciar")

            def parar(self, t):
                chamadas.append("parar")

        monkeypatch.setattr(via_banco.entrega_push, "EntregaPush", Falsa)
        monkeypatch.setattr(via_banco, "_entrega", None)
        via_banco.iniciar()
        via_banco.parar(1.0)
        assert chamadas == ["iniciar", "parar"]


class TestRepositoriosIsolados:
    @pytest.mark.parametrize("funcao", ["iniciar_borda", "iniciar_analise"])
    def test_o_banco_falso_e_recusado(self, monkeypatch, funcao):
        monkeypatch.setenv("FLOW_DB_FAKE", "1")
        with pytest.raises(RuntimeError, match="FLOW_DB_FAKE"):
            getattr(repositorios, funcao)()


# ------------------------------- desligamento ordenado -------------------------
class TestEncerrar:
    def _leitura(self, user_id=1):
        from borda import tempo
        server._enfileirar_leitura(user_id, "app", "cel1", {"x": 1}, 3, "idoso1", tempo.agora_utc())

    @pytest.fixture(autouse=True)
    def _restaurar(self):
        yield
        server._parar_flush.clear()

    def test_a_descarga_final_grava_o_que_restou_na_fila(self, banco):
        for _ in range(3):
            self._leitura()
        server.encerrar(1.0)
        assert len(banco.tabelas["leituras"]) == 3 and server._fila_leituras == []

    def test_a_descarga_final_com_banco_fora_do_ar_conta_e_nao_trava(self, banco, monkeypatch, capsys):
        self._leitura()

        def quebra(*a, **k):
            raise RuntimeError("banco fora")
        monkeypatch.setattr(repositorios.borda(), "gravar_leituras", quebra)
        server.encerrar(1.0)
        assert "1 leitura(s) nao gravada(s)" in capsys.readouterr().out

    def test_grava_o_buffer_de_agregacao_e_fecha_os_pools(self, banco):
        server._registrar_no_buffer(1, "app", {"x": 1})
        server.encerrar(1.0)
        assert len(banco.tabelas["telemetria"]) == 1
        assert repositorios.borda() is None and repositorios.analise() is None

    def test_e_idempotente(self, banco):
        server.encerrar(1.0)
        server.encerrar(1.0)

    def test_fecha_os_streams_sse(self, banco):
        class Conn:
            fechada = False

            def fechar(self):
                self.fechada = True
        c = Conn()
        HUB._conns[(1, "app")] = {"cel1": c}
        assert HUB.fechar_todas() == 1 and c.fechada
