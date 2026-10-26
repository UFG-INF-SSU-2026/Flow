"""
O contrato do `Repo`, escrito uma vez e usado por dois lados: o banco falso (tests/unit/test_repo_contrato.py) e o
PostgreSQL real (tests/banco/test_contrato_repos.py, `pytest -m banco`). Se os dois passam, o falso e uma boa imitacao.
Cada funcao recebe um `repo` vazio.
"""
from datetime import datetime, timedelta, timezone

T0 = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)


def _t(seg: int) -> datetime:
    return T0 + timedelta(seconds=seg)


def leitura(seg=0, user_id=1, fonte="app"):
    return {"instante_ingestao": _t(seg), "user_id": user_id, "device_id": "cel1", "fonte": fonte,
            "tipo_evento": "telemetria", "schema_version": 3, "instante_origem": None, "conta_id": "idoso1",
            "payload": {"x": seg, "acentuado": "luz inadequada ç"}}


def comando(msg="m1", seg=0, episodio="e1", tipo="queda.confirmacao_solicitada"):
    return {"criado_em": _t(seg), "user_id": 1, "device_id": None, "canal": "todos", "message_id": msg, "tipo": tipo,
            "payload": {"a": 1}, "expira_em": _t(seg + 7), "episodio_id": episodio}


def episodio(**extra):
    base = {"episodio_id": "e1", "user_id": 1, "device_id": None, "tipo": "IMOBILIDADE_PROLONGADA",
            "estado": "aguardando_confirmacao", "aberto_em": T0, "atualizado_em": T0, "encerrado_em": None,
            "motivo_encerramento": None, "dados": {"timeout_s": 5}}
    base.update(extra)
    return base


def leituras_em_lote_e_recentes(repo):
    assert repo.gravar_leituras([leitura(i) for i in range(3)]) == 3
    assert repo.gravar_leituras([]) == 0
    lidas = repo.ler_leituras_recentes(T0)
    assert [l["payload"]["x"] for l in lidas] == [0, 1, 2] and [l["id"] for l in lidas] == sorted(l["id"] for l in lidas)
    assert lidas[0]["payload"]["acentuado"] == "luz inadequada ç" and lidas[0]["conta_id"] == "idoso1"
    assert [l["payload"]["x"] for l in repo.ler_leituras_recentes(_t(1))] == [1, 2]
    assert [l["payload"]["x"] for l in repo.ler_leituras_recentes(T0, antes_de=_t(2))] == [0, 1]
    assert [l["payload"]["x"] for l in repo.ler_leituras_recentes(T0, limite=2)] == [1, 2]     # as mais recentes


def listar_leituras_mais_recentes_primeiro_com_filtro_e_limite(repo):
    repo.gravar_leituras([leitura(0, user_id=1), leitura(1, user_id=2), leitura(2, user_id=1), leitura(3, user_id=2)])
    assert [l["payload"]["x"] for l in repo.listar_leituras()] == [3, 2, 1, 0]                 # mais novas primeiro
    assert [l["payload"]["x"] for l in repo.listar_leituras(user_id=1)] == [2, 0]
    assert [l["payload"]["x"] for l in repo.listar_leituras(limite=2)] == [3, 2]
    assert [l["payload"]["x"] for l in repo.listar_leituras(user_id=2, limite=1)] == [3]
    assert repo.listar_leituras(user_id=9) == []


def lote_com_linha_invalida_nao_grava_nenhuma(repo, excecao):
    import pytest
    with pytest.raises(excecao):
        repo.gravar_leituras([leitura(0), leitura(1, fonte="outra")])
    assert repo.ler_leituras_recentes(T0) == []


def comando_e_idempotente_pelo_message_id(repo):
    ids = [repo.gravar_comando(comando(f"m{i}", i)) for i in range(3)]
    assert ids == sorted(ids) and None not in ids
    assert repo.gravar_comando(comando("m1", 1)) is None                  # mesmo message_id: nada regravado
    achados = repo.listar_comandos_de_episodios(["e1"])
    assert [c["message_id"] for c in achados] == ["m0", "m1", "m2"]
    assert repo.listar_comandos_de_episodios([]) == [] and repo.listar_comandos_de_episodios(["outro"]) == []


def alerta_e_confirmacao_voltam_com_id(repo):
    a = repo.gravar_alerta({"instante": T0, "user_id": 1, "device_id": "cel1", "tipo": "imobilidade",
                            "mensagem": "parado", "dados": {"k": 1}, "episodio_id": None, "schema_version": 3})
    c = repo.gravar_confirmacao({"instante": T0, "user_id": 1, "tipo": "episodio.aberto", "dados": {"k": 2},
                                 "episodio_id": "e1", "message_id": "m1", "schema_version": 3})
    assert isinstance(a, int) and isinstance(c, int)


def episodio_upsert_preserva_encerramento_e_funde_dados(repo):
    repo.gravar_episodio(episodio())
    repo.gravar_episodio(episodio(estado="encerrado", tipo="DESCONHECIDO", atualizado_em=_t(5), encerrado_em=_t(5),
                                  motivo_encerramento="CONFIRMADO", dados={"extra": 1}))
    repo.gravar_episodio(episodio(estado="encerrado", atualizado_em=_t(6), dados={}))   # nao apaga o motivo ja gravado
    (e,) = repo.listar_episodios_recentes(T0 - timedelta(days=1))
    assert (e["estado"], e["tipo"], e["motivo_encerramento"], e["encerrado_em"]) == (
        "encerrado", "IMOBILIDADE_PROLONGADA", "CONFIRMADO", _t(5))
    assert e["dados"] == {"timeout_s": 5, "extra": 1}


def episodios_recentes_incluem_os_abertos_de_qualquer_idade(repo):
    repo.gravar_episodio(episodio(episodio_id="velho-aberto", aberto_em=T0 - timedelta(days=3),
                                  atualizado_em=T0 - timedelta(days=3)))
    repo.gravar_episodio(episodio(episodio_id="velho-encerrado", estado="encerrado", aberto_em=T0 - timedelta(days=3),
                                  atualizado_em=T0 - timedelta(days=3), encerrado_em=T0 - timedelta(days=3)))
    repo.gravar_episodio(episodio(episodio_id="novo-encerrado", estado="encerrado", aberto_em=_t(-10), atualizado_em=_t(-5),
                                  encerrado_em=_t(-5)))
    achados = repo.listar_episodios_recentes(T0 - timedelta(hours=1))
    assert [e["episodio_id"] for e in achados] == ["velho-aberto", "novo-encerrado"]


def estado_do_dispositivo_upsert_e_leitura_por_device(repo):
    def estado(user, seg, valor):
        return {"user_id": user, "device_id": "bridge-dev", "atualizado_em": _t(seg), "versao_regra": 1,
                "estado": {"estado_atual": valor}}
    repo.gravar_estado_dispositivo(estado(1, 0, "NORMAL"))
    repo.gravar_estado_dispositivo(estado(1, 1, "ALERTA"))
    repo.gravar_estado_dispositivo(estado(2, 2, "NORMAL"))
    assert repo.ler_estado_por_dispositivo("bridge-dev")["user_id"] == 2             # o atualizado por ultimo
    assert repo.ler_estado_por_dispositivo("nenhum") is None
    repo.gravar_estado_dispositivo(estado(2, 3, "ALERTA"))
    assert repo.ler_estado_por_dispositivo("bridge-dev")["estado"] == {"estado_atual": "ALERTA"}


def transacao_e_tudo_ou_nada(repo):
    import pytest
    with repo.transacao() as tx:
        tx.gravar_episodio(episodio())
        tx.gravar_comando(comando())
    with pytest.raises(RuntimeError):
        with repo.transacao() as tx:
            tx.gravar_episodio(episodio(episodio_id="e2"))
            tx.gravar_comando(comando("m9", episodio="e2"))
            raise RuntimeError("falha no meio")
    assert [e["episodio_id"] for e in repo.listar_episodios_recentes(T0 - timedelta(days=1))] == ["e1"]
    assert repo.listar_comandos_de_episodios(["e2"]) == []


def contas_e_sessoes(repo):
    conta = {"id": "idoso1", "papel": "idoso", "user_ids": [1], "fontes": ["app"], "hash": "scrypt$aa$bb", "criada_em": T0}
    assert repo.criar_conta(conta) is True
    assert repo.criar_conta({**conta, "hash": "outro"}) is False                      # duplicada: recusada, nada muda
    assert repo.buscar_conta("idoso1") == {**conta, "device_ids": []} and repo.buscar_conta("nenhuma") is None
    bridge = {"id": "bridge1", "papel": "dispositivo", "user_ids": [1], "fontes": ["wokwi"], "device_ids": ["esp32-1", "esp32-2"],
              "hash": "scrypt$cc$dd", "criada_em": T0}
    assert repo.criar_conta(bridge) is True and repo.buscar_conta("bridge1") == bridge       # device_ids vai e volta

    def sessao(token, seg):
        return {"token_hash": token, "conta_id": "idoso1", "fonte": "app", "device_id": "cel1", "expira_em": _t(seg)}
    repo.gravar_sessao(sessao("a", 10))
    repo.gravar_sessao(sessao("b", 20))
    repo.gravar_sessao(sessao("c", 30))
    assert repo.consumir_sessao("a") == sessao("a", 10)
    assert repo.consumir_sessao("a") is None                                           # uso unico
    assert repo.podar_sessoes(_t(20)) == 1                                             # "b" venceu (expira_em <= agora)
    assert repo.consumir_sessao("b") is None and repo.consumir_sessao("c") == sessao("c", 30)


TODOS = [leituras_em_lote_e_recentes, listar_leituras_mais_recentes_primeiro_com_filtro_e_limite,
         comando_e_idempotente_pelo_message_id, alerta_e_confirmacao_voltam_com_id,
         episodio_upsert_preserva_encerramento_e_funde_dados, episodios_recentes_incluem_os_abertos_de_qualquer_idade,
         estado_do_dispositivo_upsert_e_leitura_por_device, transacao_e_tudo_ou_nada, contas_e_sessoes]
