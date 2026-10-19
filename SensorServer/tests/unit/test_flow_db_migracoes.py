"""Migracoes e permissoes: verificacoes ESTATICAS (sem banco). O contrato contra o Postgres real fica em tests/banco."""
import re
from pathlib import Path

import pytest

from flow_db import config, migracoes, permissoes, versao

SQL_0005 = (migracoes.DIRETORIO / "0005_permissoes_politicas.sql").read_text(encoding="utf-8")


def test_sequencia_de_migracoes_e_valida_e_comeca_em_1():
    lista = migracoes.listar()
    assert [m.versao for m in lista] == list(range(1, len(lista) + 1))


def test_versao_minima_dos_servicos_nao_passa_da_ultima_migracao():
    ultima = migracoes.ultima_disponivel()
    assert all(v <= ultima for v in versao.MIGRACAO_MINIMA.values())


def test_listar_recusa_nome_fora_do_padrao(tmp_path):
    (tmp_path / "1_curto.sql").write_text("SELECT 1;")
    with pytest.raises(migracoes.MigracaoInvalida):
        migracoes.listar(tmp_path)


def test_listar_recusa_buraco_na_sequencia(tmp_path):
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    (tmp_path / "0003_c.sql").write_text("SELECT 1;")
    with pytest.raises(migracoes.MigracaoInvalida, match="esperado 0002"):
        migracoes.listar(tmp_path)


def _grants_do_sql(sql: str) -> set[tuple[str, str, str]]:
    sem_comentarios = re.sub(r"--[^\n]*", "", sql)
    saida = set()
    for privs, tabelas, papeis in re.findall(r"GRANT\s+([A-Z, ]+?)\s+ON\s+([a-z_, ]+?)\s+TO\s+([a-z_, ]+?);",
                                             sem_comentarios, flags=re.S):
        if "USAGE" in privs:
            continue
        for papel in (x.strip() for x in papeis.split(",")):
            for tabela in (x.strip() for x in tabelas.split(",")):
                for priv in (x.strip() for x in privs.split(",")):
                    saida.add((papel, tabela, priv))
    return saida


def test_grants_da_migracao_0005_coincidem_com_a_matriz_em_python():
    assert _grants_do_sql(SQL_0005) == permissoes.matriz_como_conjunto()


def test_nenhum_grant_de_delete_fora_de_sessoes():
    assert {t for (_, t, p) in _grants_do_sql(SQL_0005) if p == "DELETE"} == {"sessoes"}


def test_tabelas_criadas_nas_migracoes_estao_todas_na_matriz():
    criadas = set()
    for m in migracoes.listar():
        criadas |= set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+([a-z_]+)", m.caminho.read_text(encoding="utf-8")))
    assert criadas <= set(permissoes.MATRIZ), criadas - set(permissoes.MATRIZ)


class TestSenhas:
    @pytest.mark.parametrize("senha", ["a" * 16, "Abc_def-1234567890", "x" * 40])
    def test_formatos_validos(self, senha):
        assert config.senha_valida(senha)

    @pytest.mark.parametrize("senha", [None, "", "curta", "tem espaco no meio 1234", "aspas'aqui-1234567890", "a" * 15,
                                       "acento-ç-1234567890"])
    def test_formatos_invalidos(self, senha):
        assert not config.senha_valida(senha)

    def test_ausente_e_invalida_nao_revelam_o_valor(self, monkeypatch):
        monkeypatch.delenv("FLOW_DB_BORDA_PASSWORD", raising=False)
        with pytest.raises(config.ConfigBanco, match="FLOW_DB_BORDA_PASSWORD"):
            config.senha_do_papel(config.PAPEL_BORDA)
        monkeypatch.setenv("FLOW_DB_BORDA_PASSWORD", "segredo errado!")
        with pytest.raises(config.ConfigBanco) as e:
            config.senha_do_papel(config.PAPEL_BORDA)
        assert "segredo errado" not in str(e.value)

    def test_cada_papel_le_so_a_propria_variavel(self, monkeypatch):
        monkeypatch.setenv("FLOW_DB_BORDA_PASSWORD", "b" * 20)
        monkeypatch.delenv("FLOW_DB_ANALISE_PASSWORD", raising=False)
        assert config.senha_do_papel(config.PAPEL_BORDA) == "b" * 20
        with pytest.raises(config.ConfigBanco):
            config.senha_do_papel(config.PAPEL_ANALISE)
