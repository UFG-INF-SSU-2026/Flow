"""
Mundo de teste do S6 (sem fixtures): banco falso com relogio explicito, a analise que AGE e fabricas de linhas.

`Mundo.reiniciar_analise()` faz o papel de "derrubar a analise e subir outra": o processo e o estado em memoria somem (um
`LacoAnalise` novo), o banco fica. Ciclos sao SINCRONOS (`executar_ciclo`): nenhum teste daqui depende de thread nem de espera.
"""
from datetime import datetime, timedelta, timezone

from analise import const as const_analise
from analise import laco
from comum import const as const_comum
from comum import mensagens
from flow_db.fake import BancoFalso

T0 = datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)
TABELAS_DE_SAIDA = ("comandos_push", "alertas", "confirmacoes", "episodios", "estado_dispositivo")


class Relogio:
    def __init__(self):
        self.agora = T0

    def __call__(self):
        return self.agora

    def ir_para(self, segundos: float):
        self.agora = T0 + timedelta(seconds=segundos)


def leitura_wokwi(t, seq, value, vmax=None, user_id=1):
    p = {"schemaVersion": const_comum.SCHEMA_VERSION_WOKWI, "eventType": "imobilidade.leitura", "deviceId": "bridge-dev",
         "entityId": "idoso-sim", "eventTimeMs": seq * 1000, "sequence": seq, "value": value, "unit": "m/s2",
         "state": "NORMAL"}
    if vmax is not None:
        p["valueMax"] = vmax
    return {"instante_ingestao": T0 + timedelta(seconds=t), "user_id": user_id, "device_id": "bridge-dev",
            "fonte": "wokwi", "tipo_evento": "imobilidade.leitura", "schema_version": const_comum.SCHEMA_VERSION_WOKWI,
            "instante_origem": None, "conta_id": "bridge1", "payload": p}


def queda_wokwi(t0=0, seq0=1):
    """Leitura normal, impacto e T_POS_IMPACTO_S de repouso: o pedido sai na ultima leitura (t0 + 4)."""
    linhas = [leitura_wokwi(t0, seq0, 1.0, 1.5), leitura_wokwi(t0 + 1, seq0 + 1, 3.0, 25.0)]
    return linhas + [leitura_wokwi(t0 + 2 + k, seq0 + 2 + k, 0.3, 0.4) for k in range(int(const_analise.T_POS_IMPACTO_S))]


def resposta(t, message_id, user_id=1, tipo="confirmacao.resposta", state="ESTOU_BEM"):
    ev = {"eventType": tipo, "state": state, "messageId": message_id, "device_id": "cel1", "source": "app"}
    return {"instante_ingestao": T0 + timedelta(seconds=t), "user_id": user_id, "device_id": "cel1", "fonte": "app",
            "tipo": tipo, "message_id": message_id, "schema_version": const_comum.SCHEMA_VERSION_APP, "payload": ev}


class Mundo:
    def __init__(self):
        self.rel = Relogio()
        self.bd = BancoFalso(relogio=self.rel)
        self.analise, self.borda = self.bd.analise(), self.bd.borda()
        self.laco = self.novo_laco()

    def novo_laco(self, **kw):
        kw.setdefault("agir", True)
        return laco.LacoAnalise(obter_repo=lambda: self.analise, relogio=self.rel, ao_decidir=lambda *_: None, **kw)

    def reiniciar_analise(self, recuperar=True):
        """Derruba a analise (some o estado em memoria; o banco fica) e sobe outra. Devolve o resultado da recuperacao."""
        self.laco = self.novo_laco()
        return self.laco.recuperar_estado() if recuperar else None

    def t(self, nome):
        return self.bd.tabelas[nome]

    def comandos(self, tipo=None):
        return [c for c in self.t("comandos_push") if tipo is None or c["tipo"] == tipo]

    def contagens(self):
        return {nome: len(self.t(nome)) for nome in TABELAS_DE_SAIDA}

    def pedido_na_analise(self):
        """Queda ja decidida pela analise (t = 4)."""
        self.borda.gravar_leituras(queda_wokwi())
        self.rel.ir_para(4)
        self.laco.executar_ciclo()
        return self.comandos(mensagens.TIPO_SOLICITADA)[0]
