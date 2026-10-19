"""Esquema real: migracoes, hypertables, politicas e permissoes (a matriz do documento = a do banco)."""
import threading

import psycopg
import pytest

from flow_db import config, conexao, migracoes, permissoes, versao

pytestmark = pytest.mark.banco


def test_migracoes_sao_idempotentes(banco_pg):
    assert migracoes.aplicar() == []


def test_duas_execucoes_simultaneas_nao_se_atropelam(banco_pg):
    erros = []

    def roda():
        try:
            migracoes.aplicar()
        except Exception as e:  # noqa: BLE001
            erros.append(e)
    ts = [threading.Thread(target=roda) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert erros == []


def test_versao_aplicada_e_a_ultima_disponivel(admin):
    assert conexao.versao_aplicada(admin) == migracoes.ultima_disponivel()
    linhas = admin.execute("SELECT versao FROM schema_migrations ORDER BY 1").fetchall()
    assert [l["versao"] for l in linhas] == list(range(1, migracoes.ultima_disponivel() + 1))


def test_servico_recusa_banco_defasado(pools):
    with pytest.raises(conexao.BancoDesatualizado, match="python -m flow_db migrar"):
        conexao.verificar_versao_minima(pools[0], migracoes.ultima_disponivel() + 1, "borda")
    for servico, minima in versao.MIGRACAO_MINIMA.items():
        assert conexao.verificar_versao_minima(pools[0], minima, servico) >= minima


def test_tabelas_de_series_sao_hypertables(admin):
    nomes = {l["hypertable_name"] for l in admin.execute(
        "SELECT hypertable_name FROM timescaledb_information.hypertables").fetchall()}
    assert {"telemetria", "leituras", "respostas_dispositivo", "entregas_push", "comandos_push"} <= nomes


def test_politicas_de_compressao_e_retencao(admin):
    achadas = {(l["hypertable_name"], l["proc_name"]) for l in admin.execute(
        "SELECT hypertable_name, proc_name FROM timescaledb_information.jobs"
        " WHERE proc_name IN ('policy_retention', 'policy_compression')").fetchall()}
    esperadas = {("leituras", "policy_compression"), ("telemetria", "policy_compression")} | {
        (t, "policy_retention") for t in ("leituras", "telemetria", "respostas_dispositivo", "entregas_push",
                                          "comandos_push")}
    assert esperadas <= achadas


def test_grants_reais_coincidem_com_a_matriz(admin):
    linhas = admin.execute(
        "SELECT grantee, table_name, privilege_type FROM information_schema.role_table_grants"
        " WHERE table_schema = 'public' AND grantee = ANY(%s)", (list(config.PAPEIS),)).fetchall()
    reais = {(l["grantee"], l["table_name"], l["privilege_type"]) for l in linhas}
    assert reais == permissoes.matriz_como_conjunto()


def test_papeis_sao_limitados(admin):
    for papel in config.PAPEIS:
        p = admin.execute("SELECT rolsuper, rolcreatedb, rolcreaterole, rolconnlimit FROM pg_roles WHERE rolname = %s",
                          (papel,)).fetchone()
        assert (p["rolsuper"], p["rolcreatedb"], p["rolcreaterole"]) == (False, False, False)
        assert p["rolconnlimit"] == config.limite_conexoes(papel)


def _como(papel: str, comando: str):
    with psycopg.connect(config.dsn_papel(papel), autocommit=True) as conn:
        conn.execute(comando)


@pytest.mark.parametrize("papel,comando", [
    (config.PAPEL_BORDA, "INSERT INTO comandos_push (criado_em, user_id, canal, message_id, tipo, payload)"
                         " VALUES (now(), 1, 'app', 'm', 't', '{}')"),
    (config.PAPEL_BORDA, "INSERT INTO alertas (user_id, tipo) VALUES (1, 'x')"),
    (config.PAPEL_BORDA, "UPDATE cursor_analise SET ultimo_leitura_id = 1"),
    (config.PAPEL_ANALISE, "INSERT INTO leituras (instante_ingestao, user_id, device_id, fonte, schema_version, payload)"
                           " VALUES (now(), 1, 'd', 'app', 3, '{}')"),
    (config.PAPEL_ANALISE, "UPDATE cursor_borda SET ultimo_comando_id = 1"),
    (config.PAPEL_ANALISE, "DELETE FROM episodios"),
    (config.PAPEL_BORDA, "DELETE FROM leituras"),
    (config.PAPEL_BORDA, "CREATE TABLE intruso (x int)"),
    (config.PAPEL_ANALISE, "DROP TABLE leituras"),
])
def test_papel_nao_faz_o_que_nao_deve(admin, papel, comando):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _como(papel, comando)
