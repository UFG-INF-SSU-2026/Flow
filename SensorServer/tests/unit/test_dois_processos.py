"""Os dois processos: ambiente por servico, pontos de entrada, API da analise (token, rotas) e desligamento ordenado da borda."""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from _apoio import SEGREDO, ambiente_tls
from analise import api
from borda import server
from borda.sse_hub import HUB
from comum import const as const_comum
from comum.contrato_analise import (Alerta, Comando, Confirmacao, Decisoes, Descarte, Leitura, MudancaEpisodio, Resposta,
                                    ResumoEstado, de_json_item)
from flow_db import repositorios

RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # SensorServer/
TOKEN = "t" * 24


def _rodar(args, tmp_path, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("FLOW_")}
    env.update({"FLOW_SEM_DOTENV": "1", "PYTHONPATH": RAIZ, **ambiente_tls()})   # TLS: certificado de teste do projeto
    env.update(extra)
    return subprocess.run([sys.executable, *args], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)


# ------------------------------- ambiente e pontos de entrada -------------------------
class TestProcessos:
    def test_servico_desconhecido_recusa(self, tmp_path):
        r = _rodar(["-c", "from comum import const"], tmp_path, FLOW_SERVICO="outro")
        assert r.returncode != 0 and "FLOW_SERVICO" in r.stderr

    def test_os_pontos_de_entrada_recusam_o_uso_direto(self):
        from analise import servico as servico_analise
        from borda import servico as servico_borda
        assert servico_borda.main() == 1 and servico_analise.main() == 1     # FLOW_SERVICO nao esta definido nos testes

    def test_a_analise_sem_token_recusa(self, tmp_path):
        r = _rodar(["-m", "analise"], tmp_path, FLOW_SERVICO="analise")
        assert r.returncode == 1 and "FLOW_ANALISE_TOKEN" in r.stderr

    def test_a_borda_sem_banco_recusa_sem_vazar_senha(self, tmp_path):
        r = _rodar(["-m", "borda"], tmp_path, FLOW_JWT_SECRET=SEGREDO, FLOW_ANALISE_TOKEN=TOKEN, FLOW_DB_PORT="1",
                   FLOW_DB_PASSWORD="b" * 20)
        assert r.returncode == 1
        assert "banco" in r.stderr.lower() and "b" * 20 not in r.stderr

    def test_a_borda_nunca_carrega_a_analise(self, tmp_path):
        """Importa o servidor da borda e confere que nenhum modulo `analise*` foi carregado."""
        sonda = ("import sys; from borda import server; "
                 "print(sorted(m for m in sys.modules if m == 'analise' or m.startswith('analise.')))")
        r = _rodar(["-c", sonda], tmp_path, FLOW_SEM_THREADS="1", FLOW_DB_FAKE="1", FLOW_JWT_SECRET=SEGREDO)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "[]"


# ------------------------------- API da analise -------------------------
@pytest.fixture
def app_analise():
    return api.criar_app(api.Analise(), TOKEN).test_client()


def _cab(token=TOKEN):
    return {const_comum.ANALISE_CABECALHO_TOKEN: token}


class TestApiDaAnalise:
    @pytest.mark.parametrize("token", [None, "", "errado", "t" * 23])
    def test_token_ausente_ou_errado_e_recusado_em_todas_as_rotas(self, app_analise, token):
        cab = {} if token is None else _cab(token)
        for metodo, rota in (("get", "/saude"), ("post", "/analisar"), ("post", "/vigiar"), ("post", "/restaurar")):
            assert getattr(app_analise, metodo)(rota, headers=cab, json={}).status_code == 401

    def test_saude_devolve_o_boot_id(self, app_analise):
        r = app_analise.get("/saude", headers=_cab())
        assert r.status_code == 200 and r.get_json()["ok"] is True and len(r.get_json()["boot_id"]) == 32

    def test_lote_entra_decisoes_saem(self, app_analise):
        leitura = {"tipo": "leitura", "instante": datetime.now(timezone.utc).isoformat(), "user_id": 1, "device_id": "bridge-dev",
                   "fonte": "wokwi", "schema_version": 3,
                   "payload": {"schemaVersion": 3, "eventType": "imobilidade.leitura", "deviceId": "bridge-dev",
                               "entityId": "x", "eventTimeMs": 1000, "sequence": 1, "value": 0.3, "valueMax": 0.4,
                               "unit": "m/s2", "state": "NORMAL"}}
        r = app_analise.post("/analisar", headers=_cab(), json={"itens": [leitura, {"lixo": 1}]})   # o item ruim e pulado
        assert r.status_code == 200
        corpo = r.get_json()
        assert set(corpo["decisoes"]) == {"comandos", "alertas", "confirmacoes", "estados", "descartes"}
        assert [e["device_id"] for e in corpo["decisoes"]["estados"]] == ["bridge-dev"]

    @pytest.mark.parametrize("rota,corpo", [("/analisar", {}), ("/analisar", {"itens": "x"}), ("/vigiar", {"agora": "ontem"}),
                                            ("/restaurar", {"leituras": []})])
    def test_corpo_fora_do_contrato_da_400(self, app_analise, rota, corpo):
        assert app_analise.post(rota, headers=_cab(), json=corpo).status_code == 400

    def test_rota_desconhecida_da_404(self, app_analise):
        assert app_analise.get("/nada", headers=_cab()).status_code == 404

    def test_restaurar_vazio_responde_ok_e_o_boot_id_nao_muda(self, app_analise):
        antes = app_analise.get("/saude", headers=_cab()).get_json()["boot_id"]
        r = app_analise.post("/restaurar", headers=_cab(), json={"leituras": [], "episodios": [], "mensagens": []})
        assert r.status_code == 200 and r.get_json()["boot_id"] == antes

    def test_vigiar_sem_episodios_devolve_decisoes_vazias(self, app_analise):
        r = app_analise.post("/vigiar", headers=_cab(), json={"agora": "2026-10-19T12:00:00+00:00"})
        assert r.status_code == 200 and not any(r.get_json()["decisoes"].values())


_T = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
_EPISODIO = MudancaEpisodio("e1", 1, None, "IMOBILIDADE_PROLONGADA", "aguardando_confirmacao", _T, _T, None, None, {"k": 1})
_IDA_E_VOLTA = {     # nome -> (objeto, funcao que le o JSON de volta)
    "leitura": (Leitura(_T, 1, "bridge-dev", "wokwi", {"eventType": "imobilidade.leitura", "value": 0.3}, 3), de_json_item),
    "resposta": (Resposta(_T, 1, "cel1", "app", {"eventType": "push.ack", "state": "ESTOU_BEM", "messageId": "m1"}, 3),
                 de_json_item),
    "decisoes": (Decisoes([Comando(1, "cel1", {"messageId": "m1", "messageType": "x"}, _T, _T, _EPISODIO)],
                          [Alerta(1, "cel1", "imobilidade", {"mensagem": "a"}, 3, _T)],
                          [Confirmacao(1, "episodio.aberto", {"a": 1}, _T)],
                          [ResumoEstado(1, "cel1", _T, 1, {"estado_atual": "NORMAL"})],
                          [Descarte(1, None, "motivo", "d")]), Decisoes.de_json),
}


@pytest.mark.parametrize("nome", _IDA_E_VOLTA)
def test_o_contrato_vai_e_volta_pelo_json(nome):
    objeto, de_json = _IDA_E_VOLTA[nome]
    assert de_json(json.loads(json.dumps(objeto.para_json()))) == objeto


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
        monkeypatch.setattr(repositorios.repo(), "gravar_leituras", quebra)
        server.encerrar(1.0)
        assert "1 leitura(s) nao gravada(s)" in capsys.readouterr().out

    def test_fecha_o_repositorio(self, banco):
        server.encerrar(1.0)
        assert repositorios.repo() is None

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
