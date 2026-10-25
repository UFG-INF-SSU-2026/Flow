"""Migracoes e configuracao do banco: verificacoes ESTATICAS (sem banco). O contrato contra o Postgres real fica em tests/banco."""
import re

import pytest

from _apoio import ambiente_tls
from flow_db import config, migracoes, versao


def test_sequencia_de_migracoes_e_valida_e_comeca_em_1():
    lista = migracoes.listar()
    assert [m.versao for m in lista] == list(range(1, len(lista) + 1))


def test_versao_minima_nao_passa_da_ultima_migracao():
    assert versao.MIGRACAO_MINIMA <= migracoes.ultima_disponivel()


def test_listar_recusa_nome_fora_do_padrao(tmp_path):
    (tmp_path / "1_curto.sql").write_text("SELECT 1;")
    with pytest.raises(migracoes.MigracaoInvalida):
        migracoes.listar(tmp_path)


def test_listar_recusa_buraco_na_sequencia(tmp_path):
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    (tmp_path / "0003_c.sql").write_text("SELECT 1;")
    with pytest.raises(migracoes.MigracaoInvalida, match="esperado 0002"):
        migracoes.listar(tmp_path)


def test_o_esquema_so_tem_as_tabelas_que_a_borda_usa():
    criadas = set()
    for m in migracoes.listar():
        criadas |= set(re.findall(r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?([a-z_]+)", m.caminho.read_text(encoding="utf-8")))
    assert criadas == {"leituras", "alertas", "confirmacoes", "episodios", "comandos_push", "estado_dispositivo"}


class TestConfig:
    def test_senha_ausente_nao_gera_dsn(self, monkeypatch):
        monkeypatch.delenv("FLOW_DB_PASSWORD", raising=False)
        with pytest.raises(config.ConfigBanco, match="FLOW_DB_PASSWORD"):
            config.dsn()

    def test_dsn_usa_o_usuario_e_a_senha_do_ambiente(self, monkeypatch):
        monkeypatch.setenv("FLOW_DB_PASSWORD", "Senha_ok-123")
        monkeypatch.setenv("FLOW_DB_USER", "alguem")
        for nome, valor in ambiente_tls().items():
            monkeypatch.setenv(nome, valor)
        dsn = config.dsn()
        assert "Senha_ok-123" in dsn and "alguem" in dsn

    def test_porta_invalida_e_recusada(self, monkeypatch):
        monkeypatch.setenv("FLOW_DB_PORT", "abc")
        with pytest.raises(config.ConfigBanco, match="FLOW_DB_PORT"):
            config.porta()
