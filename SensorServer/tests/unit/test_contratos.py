"""Contratos de schemas/: carregam, validam as fixtures de cada versao, concordam com const.py e com docs/schemas.md,
e o que o servidor produz (push, alertas, confirmacoes) obedece ao contrato. Mais o validador de schemas em si."""
import json
import re
from pathlib import Path

import pytest

import const
import mensagens
import regra_imobilidade as imob
import regra_luz as luz
import regra_queda
from comum import contratos, schemas

RAIZ = Path(__file__).resolve().parents[2]
FIXTURES = json.loads((RAIZ / "tests" / "fixtures" / "contratos.json").read_text(encoding="utf-8"))
DOC_SCHEMAS = RAIZ.parent / "docs" / "schemas.md"


# ------------------------------------------------------------ carga e catalogo
class TestCarga:
    def test_todos_os_schemas_carregam(self):
        nomes = schemas.nomes()
        assert nomes, "nenhum schema encontrado"
        for nome in nomes:
            assert isinstance(schemas.carregar(nome), dict)

    def test_catalogo_e_arquivos_coincidem(self):
        assert sorted(schemas.catalogo()) == schemas.nomes()

    def test_toda_fixture_tem_contrato_e_todo_contrato_tem_fixture(self):
        assert sorted(FIXTURES) == schemas.nomes()

    def test_contrato_desconhecido(self):
        with pytest.raises(schemas.ContratoDesconhecido):
            schemas.carregar("nao-existe")

    def test_palavra_chave_nao_suportada_e_recusada(self, tmp_path, monkeypatch):
        (tmp_path / "ruim.json").write_text('{"type": "object", "additionalProperties": false}', encoding="utf-8")
        monkeypatch.setattr(schemas, "PASTA", tmp_path)
        with pytest.raises(schemas.SchemaInvalido):
            schemas.carregar("ruim")


# ------------------------------------------------------------ fixtures por contrato
CASOS_VALIDOS = [(c, n, o) for c, g in FIXTURES.items() for n, o in g["validos"].items()]
CASOS_INVALIDOS = [(c, n, o) for c, g in FIXTURES.items() for n, o in g["invalidos"].items()]


@pytest.mark.parametrize("contrato,nome,obj", CASOS_VALIDOS, ids=[f"{c}:{n}" for c, n, _ in CASOS_VALIDOS])
def test_fixture_valida_passa(contrato, nome, obj):
    assert contratos.validar(contrato, obj) == []


@pytest.mark.parametrize("contrato,nome,obj", CASOS_INVALIDOS, ids=[f"{c}:{n}" for c, n, _ in CASOS_INVALIDOS])
def test_fixture_invalida_e_rejeitada(contrato, nome, obj):
    assert contratos.validar(contrato, obj)


def test_cada_contrato_tem_fixture_valida_e_invalida():
    for contrato, grupo in FIXTURES.items():
        assert grupo["validos"] and grupo["invalidos"], contrato


def test_wokwi_tem_fixture_de_cada_versao():
    nomes = FIXTURES["wokwi-telemetria"]["validos"]
    assert any(n.startswith("v1") for n in nomes) and any(n.startswith("v2") for n in nomes) \
        and any(n.startswith("v3") for n in nomes)


def test_app_tem_fixture_de_cada_versao():
    nomes = FIXTURES["app-leitura-ambiente"]["validos"]
    assert {"v1_sem_schemaVersion", "v2", "v3"} <= set(nomes)


# ------------------------------------------------------------ versoes e documentacao
class TestVersoes:
    def test_x_versao_concorda_com_const(self):
        for nome, meta in schemas.catalogo().items():
            const_nome = meta["versao_const"]
            documentada = contratos.versao_documentada(nome)
            if const_nome is None:
                assert documentada is None, f"{nome}: tem x-versao mas o catalogo nao aponta constante"
            else:
                assert documentada == getattr(const, const_nome), f"{nome}: x-versao diferente de const.{const_nome}"

    def test_tabela_de_versoes_do_docs_concorda_com_os_schemas(self):
        texto = DOC_SCHEMAS.read_text(encoding="utf-8")
        tabela = texto.split("## Versões atuais")[1].split("**Como evoluir")[0]
        doc = {m.group(1): int(m.group(2)) for m in re.finditer(r"^\| (\d+(?:\.\d+)?) \|.*\| (\d+) \|$", tabela, re.M)}
        assert doc, "tabela de versoes nao encontrada em docs/schemas.md"
        conferidos = 0
        for nome, meta in schemas.catalogo().items():
            if meta["versao_const"] and meta["secao"] in doc:
                assert doc[meta["secao"]] == contratos.versao_documentada(nome), f"{nome}: docs/schemas.md diverge"
                conferidos += 1
        assert conferidos >= 8

    def test_toda_secao_do_catalogo_existe_no_docs(self):
        texto = DOC_SCHEMAS.read_text(encoding="utf-8")
        for nome, meta in schemas.catalogo().items():
            if meta["secao"] and meta["secao"] != "0":
                assert re.search(rf"^#+ {re.escape(meta['secao'])}[. ]", texto, re.M), f"{nome}: secao {meta['secao']}"

    def test_fontes_validas_do_const_e_do_schema(self):
        assert set(contratos.enum("roteamento", "source")) == const.FONTES_VALIDAS


class TestVocabulario:
    def test_estados_do_wokwi(self):
        e = contratos.estados_por_evento("wokwi-telemetria")
        assert e["imobilidade.leitura"] == {"NORMAL", "AGUARDANDO_CONFIRMACAO", "ALERTA_CONFIRMADO"}
        assert e["imobilidade.decisao"] == {"LEITURA_INVALIDA", "LEITURA_FORA_DE_FAIXA", "SUSPEITA_IMOBILIDADE",
                                            "CONFIRMADO_OK", "MOVIMENTO_RETOMADO", "ALERTA_IMOBILIDADE", "REARMADO_MANUAL"}

    @pytest.mark.parametrize("contrato", ["wokwi-eventos-retorno", "app-eventos-retorno"])
    def test_estados_de_retorno(self, contrato):
        assert contratos.estados_por_evento(contrato) == {"push.ack": {"ATIVADO", "REJEITADO"},
                                                          "confirmacao.resposta": {"ESTOU_BEM", "SEM_RESPOSTA"}}

    def test_estados_que_encerram_alerta_existem_no_vocabulario(self):
        decisoes = contratos.estados_por_evento("wokwi-telemetria")["imobilidade.decisao"]
        assert imob.ESTADOS_QUE_ENCERRAM_ALERTA <= decisoes

    def test_obrigatorios_do_wokwi(self):
        assert set(contratos.obrigatorios("wokwi-telemetria")) == {
            "eventType", "deviceId", "entityId", "eventTimeMs", "sequence", "value", "unit", "state"}


# ------------------------------------------------------------ o que o servidor produz obedece ao contrato
class TestSaidasDoServidor:
    def test_mensagens_de_push(self):
        assert contratos.validar("push-mensagem", mensagens.criar_pedido_confirmacao(
            1, "ep1", 5, const.MOTIVO_A)) == []
        assert contratos.validar("push-mensagem", mensagens.criar_pedido_confirmacao(
            1, "ep1", 5, const.MOTIVO_B)) == []
        for resultado in sorted(mensagens.RESULTADOS_ENCERRAMENTO):
            assert contratos.validar("push-mensagem", mensagens.criar_encerramento(1, "ep1", resultado)) == []

    def test_mensagem_pronta_para_envio(self):
        msg = mensagens.criar_pedido_confirmacao(1, "ep1", 5, const.MOTIVO_A)
        assert contratos.validar("push-mensagem", mensagens.para_envio(msg)) == []

    def test_alerta_de_luz(self):
        alerta = None
        for seq in range(1, const.LEITURAS_CONSECUTIVAS_PARA_ALERTA + 1):
            alerta = luz.avaliar_regra(1, {"device_id": "d1", "event_time": "2026-10-14T22:00:00-03:00",
                                           "luminosidade": 10, "cct": const.CCT_LIMIAR_FRIO + 500, "seq_num": seq}) or alerta
        assert alerta is not None and contratos.validar("alerta-luz", alerta) == []

    def test_alerta_de_imobilidade(self):
        alerta = imob.processar_evento(1, {"eventType": "imobilidade.decisao", "state": "ALERTA_IMOBILIDADE",
                                           "deviceId": "d1", "entityId": "e1", "eventTimeMs": 1000, "sequence": 1,
                                           "value": 0.3, "unit": "m/s2"})
        assert alerta is not None and contratos.validar("alerta-imobilidade", alerta) == []

    def test_registros_de_confirmacao(self, monkeypatch):
        linhas = []
        monkeypatch.setattr(regra_queda, "OUVINTE_REGISTRO", lambda uid, tipo, linha: linhas.append(linha))
        pedido = regra_queda.abrir_por_imobilidade(1)
        regra_queda.registrar(1, "push.entrega", {"episodeId": pedido.episode_id, "messageId": "m1", "canal": "app",
                                                   "resultado": "ENTREGUE_AO_CANAL"})
        regra_queda.vigiar(agora=regra_queda._agora() + 1000)
        assert len(linhas) >= 3
        for linha in linhas:
            assert contratos.validar("confirmacao-registro", linha) == [], linha


# ------------------------------------------------------------ o validador
class TestValidador:
    def test_integer_nao_aceita_bool_nem_float(self):
        s = {"type": "integer"}
        assert schemas.validar(s, 3) == []
        for ruim in (True, False, 3.0, "3", None, [3]):
            assert schemas.validar(s, ruim), ruim

    def test_number_nao_aceita_bool_nem_nao_finito(self):
        s = {"type": "number"}
        assert schemas.validar(s, 3) == [] and schemas.validar(s, 3.5) == []
        for ruim in (True, "3", None, float("nan"), float("inf")):
            assert schemas.validar(s, ruim), ruim

    def test_enum_e_estrito_com_booleano(self):
        s = {"enum": [1, "a"]}
        assert schemas.validar(s, 1) == [] and schemas.validar(s, True)

    def test_const(self):
        assert schemas.validar({"const": "x"}, "x") == [] and schemas.validar({"const": "x"}, "y")

    def test_required_e_properties(self):
        s = {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}}
        assert schemas.validar(s, {"a": 1}) == []
        assert schemas.validar(s, {}) == ["$.a: obrigatorio"]
        assert schemas.validar(s, {"a": "1"})

    def test_propriedade_desconhecida_e_aceita(self):
        assert schemas.validar({"type": "object", "properties": {"a": {"type": "integer"}}}, {"a": 1, "novo": [1]}) == []

    def test_tipo_alternativo(self):
        s = {"type": ["string", "null"]}
        assert schemas.validar(s, None) == [] and schemas.validar(s, "x") == [] and schemas.validar(s, 1)

    def test_limites(self):
        assert schemas.validar({"minimum": 1, "maximum": 3}, 2) == []
        assert schemas.validar({"minimum": 1}, 0) and schemas.validar({"maximum": 3}, 4)
        assert schemas.validar({"type": "string", "minLength": 1}, "") and schemas.validar({"type": "string", "maxLength": 2}, "abc")

    def test_pattern(self):
        assert schemas.validar({"type": "string", "pattern": "^a+$"}, "aaa") == []
        assert schemas.validar({"type": "string", "pattern": "^a+$"}, "b")

    def test_any_of_all_of_if_then(self):
        assert schemas.validar({"anyOf": [{"type": "integer"}, {"type": "string"}]}, "x") == []
        assert schemas.validar({"anyOf": [{"type": "integer"}, {"type": "string"}]}, None)
        cond = {"if": {"required": ["t"], "properties": {"t": {"const": 1}}}, "then": {"required": ["x"]}}
        assert schemas.validar(cond, {"t": 1}) and schemas.validar(cond, {"t": 1, "x": 0}) == []
        assert schemas.validar(cond, {"t": 2}) == [] and schemas.validar(cond, {}) == []

    def test_erro_nunca_traz_o_valor_recebido(self):
        segredo = "SEGREDO-NAO-VAZAR"
        for contrato, campo in (("wokwi-telemetria", "state"), ("wokwi-telemetria", "value"), ("roteamento", "source"),
                                ("auth-login", "fonte")):
            obj = dict(FIXTURES[contrato]["validos"][next(iter(FIXTURES[contrato]["validos"]))])
            obj[campo] = segredo
            erros = contratos.validar(contrato, obj)
            assert erros and all(segredo not in e for e in erros)