"""
Comparacao em MODO SOMBRA (roadmap do split, S3b): o caminho direto (que age) e o laco da analise por cursor (que so
registra) decidem sobre os MESMOS dados; este modulo guarda as decisoes dos dois lados e diz se sao iguais.

O QUE E COMPARADO. Cada `nucleo.Decisoes` vira uma lista de `Registro` (uma decisao cada: comando de push, episodio,
alerta, registro de confirmacao, resumo de estado, descarte). A identidade de um registro (`chave`) e o que a decisao
DIZ, nunca o que e sorteado ou carimbado: `messageId` e `episodeId` (uuid), `timestamp` e o instante ficam de fora da
chave. O instante e comparado a parte, dentro de uma tolerancia (ver `tolerancia_instante`).

  - decisoes vindas de leitura ou resposta: os dois lados usam o MESMO instante de ingestao (o que a borda grava em
    `leituras` / `respostas_dispositivo`), entao a tolerancia e so folga (const_analise.ORCAMENTO_LATENCIA_S);
  - decisoes do vigia: cada lado usa o seu relogio real e o seu periodo, entao a tolerancia soma o periodo do vigia.

COMO OS REGISTROS SE PAREIAM. Por (usuario, tipo, chave), na ordem de chegada: a i-esima decisao do caminho direto
com a i-esima da sombra. Sobra do direto = `faltando_na_sombra`; sobra da sombra = `a_mais_na_sombra`; par com origem
diferente (por exemplo uma encerrou por resposta e a outra por expiracao) = `origem_diferente`; par fora da tolerancia
= `instante_fora_da_tolerancia`. Decisao da sombra marcada como RECONSTRUCAO (linha mais velha que
const_analise.IDADE_MAX_ACAO_S) nao e comparavel: o caminho direto nunca a viu como dado novo. Fica contada a parte.
O resumo de estado e comparado pela SEQUENCIA de assinaturas distintas por dispositivo (o caminho direto so grava
quando a assinatura muda; repetir a mesma assinatura nao e decisao).

IDS DOS PEDIDOS. Cada lado sorteia os seus `messageId`/`episodeId`, e o dispositivo so conhece os do caminho direto
(e quem entrega o push). Sem ajuda, o laco nunca reconheceria a resposta do dispositivo ("Estou bem", push.ack) e
toda confirmacao divergiria. Por isso a `Sombra` guarda, por usuario, os `messageId` dos pedidos de cada lado em ordem
e `traduzir_resposta` troca, na entrada do laco, o id do caminho direto pelo do pedido correspondente do laco (o i-esimo
de um e o i-esimo do outro). Um id que o caminho direto nao emitiu segue como esta: e desconhecido para os dois.
So existe na sombra: na S4 a borda entrega os comandos da propria analise e os ids coincidem.

ATRASO. Para os pares leitura/resposta, `visto_em` (relogio de parede, no momento em que cada lado registrou) mede
quanto a sombra demora depois do caminho direto. O criterio da S3b usa o p95 contra const_analise.ORCAMENTO_LATENCIA_S.

Sem Flask e sem `borda` (conferido por tests/unit/test_fronteiras.py). Nunca levanta no caminho dos dados: falha de
gravacao do arquivo vai para o log e a coleta segue.
"""
import json
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Callable, Iterable

from analise import const as const_analise
from analise import nucleo
from comum import const as const_comum
from comum import mensagens

LADO_DIRETO = "direto"
LADO_SOMBRA = "sombra"

ORIGEM_LEITURA = "leitura"
ORIGEM_RESPOSTA = "resposta"
ORIGEM_VIGIA = "vigia"

T_COMANDO = "comando"
T_EPISODIO = "episodio"
T_ALERTA = "alerta"
T_CONFIRMACAO = "confirmacao"
T_ESTADO = "estado"
T_DESCARTE = "descarte"
TIPOS = (T_COMANDO, T_EPISODIO, T_ALERTA, T_CONFIRMACAO, T_ESTADO, T_DESCARTE)

D_FALTANDO = "faltando_na_sombra"
D_A_MAIS = "a_mais_na_sombra"
D_INSTANTE = "instante_fora_da_tolerancia"
D_ORIGEM = "origem_diferente"

# Campos de `confirmacoes.dados` que dizem algo sobre a decisao (o resto e id sorteado ou carimbo de tempo).
_CAMPOS_CONFIRMACAO = ("motivo", "resultado", "state", "eventType", "source", "mensagem_desconhecida", "tardia",
                       "fonte_do_disparo", "versao_referencia")


# ============================================================================
# Registro
# ============================================================================
@dataclass(frozen=True)
class Registro:
    """Uma decisao de um dos lados. `chave` e JSON canonico (sem ids sorteados nem instantes)."""
    lado: str                       # LADO_DIRETO | LADO_SOMBRA
    origem: str                     # ORIGEM_LEITURA | ORIGEM_RESPOSTA | ORIGEM_VIGIA
    tipo: str                       # um de TIPOS
    user_id: int
    chave: str
    instante: datetime | None       # instante da DECISAO (None no descarte, que nao tem instante proprio)
    visto_em: float | None = None   # relogio de parede (epoca, s) de quando este lado registrou; None fora do ar
    reconstrucao: bool = False      # so a sombra: decisao sobre linha antiga (nao age, nao entra na comparacao)

    def como_json(self) -> dict:
        return {"lado": self.lado, "origem": self.origem, "tipo": self.tipo, "user_id": self.user_id,
                "chave": self.chave, "instante": self.instante.isoformat() if self.instante else None,
                "visto_em": self.visto_em, "reconstrucao": self.reconstrucao}

    @staticmethod
    def de_json(d: dict) -> "Registro":
        instante = datetime.fromisoformat(d["instante"]) if d.get("instante") else None
        return Registro(d["lado"], d["origem"], d["tipo"], int(d["user_id"]), d["chave"], instante,
                        d.get("visto_em"), bool(d.get("reconstrucao", False)))


def _canonico(valor) -> str:
    return json.dumps(valor, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _fontes(lista) -> list:
    """Fontes usadas/desatualizadas na fusao, em ordem estavel (a ordem do dict de leituras nao e decisao)."""
    if not isinstance(lista, list):
        return []
    return sorted(([f.get("fonte"), f.get("device_id"), f.get("versao"), f.get("degradada")]
                   for f in lista if isinstance(f, dict)), key=_canonico)


def _dados_da_confirmacao(dados: dict) -> dict:
    estavel = {k: dados[k] for k in _CAMPOS_CONFIRMACAO if k in dados}
    estavel["episodio_conhecido"] = dados.get("episodeId") is not None
    if "fontes_usadas" in dados:
        estavel["fontes_usadas"] = _fontes(dados["fontes_usadas"])
    if "fontes_desatualizadas" in dados:
        estavel["fontes_desatualizadas"] = _fontes(dados["fontes_desatualizadas"])
    return estavel


def registros_de(decisoes: nucleo.Decisoes, lado: str, origem: str, reconstrucao: bool = False,
                 visto_em: float | None = None) -> list[Registro]:
    """`Decisoes` -> registros comparaveis (uma por comando, episodio, alerta, confirmacao, estado e descarte)."""
    def reg(tipo, user_id, chave, instante):
        return Registro(lado, origem, tipo, user_id, _canonico(chave), instante, visto_em, reconstrucao)

    saida: list[Registro] = []
    for c in decisoes.comandos:
        prazo = None if c.expira_em is None else round((c.expira_em - c.criado_em).total_seconds(), 3)
        saida.append(reg(T_COMANDO, c.user_id, {"messageType": c.mensagem["messageType"], "payload": c.mensagem["payload"],
                                               "device_id": c.device_id, "prazo_s": prazo}, c.criado_em))
        e = c.episodio
        saida.append(reg(T_EPISODIO, e.user_id, {"tipo": e.tipo, "estado": e.estado, "device_id": e.device_id,
                                                "motivo_encerramento": e.motivo_encerramento, "dados": e.dados},
                         e.atualizado_em))
    for a in decisoes.alertas:
        saida.append(reg(T_ALERTA, a.user_id, {"tipo": a.tipo, "device_id": a.device_id, "dados": a.dados,
                                              "schema_version": a.schema_version}, a.instante))
    for c in decisoes.confirmacoes:
        saida.append(reg(T_CONFIRMACAO, c.user_id, {"tipo": c.tipo, "dados": _dados_da_confirmacao(c.dados)}, c.instante))
    for r in decisoes.estados:
        saida.append(reg(T_ESTADO, r.user_id, {"device_id": r.device_id, "estado_atual": r.estado.get("estado_atual"),
                                              "ultima_decisao": r.estado.get("ultima_decisao"),
                                              "alerta_ativo": r.estado.get("alerta_ativo")}, r.instante))
    for d in decisoes.descartes:
        saida.append(reg(T_DESCARTE, d.user_id, {"motivo": d.motivo, "detalhe": d.detalhe, "device_id": d.device_id}, None))
    return saida


# ============================================================================
# Coleta (roda no processo do servidor)
# ============================================================================
class Sombra:
    """Guarda as decisoes dos dois lados (em memoria, com teto) e, se tiver `caminho`, uma linha JSON por decisao.
    Seguro entre threads: o caminho direto registra na thread da requisicao e o laco na sua thread."""

    def __init__(self, caminho: str | None = None, relogio: Callable[[], float] = time.time,
                 max_registros: int | None = None):
        self._caminho = caminho
        self._relogio = relogio
        self._registros: deque[Registro] = deque(
            maxlen=const_analise.SOMBRA_MAX_REGISTROS if max_registros is None else max_registros)
        self._lock = threading.Lock()
        self._falha_avisada = False
        self._ids: dict[tuple, list[str]] = {}      # (lado, user_id) -> messageId dos pedidos, na ordem de emissao

    def registrar(self, lado: str, decisoes: nucleo.Decisoes, origem: str, reconstrucao: bool = False) -> list[Registro]:
        """Registra as decisoes de um lado. Nunca levanta."""
        try:
            novos = registros_de(decisoes, lado, origem, reconstrucao, self._relogio())
        except Exception as e:  # noqa: BLE001  (a sombra nunca pode atrapalhar quem decide)
            print(f"  [sombra] falha ao preparar os registros (ignorada): {type(e).__name__}: {str(e)[:200]}")
            return []
        if not novos:
            return []
        with self._lock:
            self._registros.extend(novos)
            if not reconstrucao:        # reconstrucao nao tem par no caminho direto: nao pode entrar na correspondencia
                for c in decisoes.comandos:
                    if c.mensagem.get("messageType") == mensagens.TIPO_SOLICITADA:
                        self._ids.setdefault((lado, c.user_id), []).append(c.mensagem["messageId"])
            self._gravar(novos)
        return novos

    def traduzir_resposta(self, resposta: nucleo.Resposta) -> nucleo.Resposta:
        """Resposta do dispositivo com o `messageId` do pedido do CAMINHO DIRETO -> a mesma resposta com o `messageId`
        do pedido correspondente do laco (ver "IDS DOS PEDIDOS" no topo). Nao altera mais nada."""
        mid = resposta.evento.get("messageId")
        with self._lock:
            do_direto = self._ids.get((LADO_DIRETO, resposta.user_id), [])
            da_sombra = self._ids.get((LADO_SOMBRA, resposta.user_id), [])
            if mid not in do_direto:
                return resposta
            i = do_direto.index(mid)
            novo = da_sombra[i] if i < len(da_sombra) else f"sem-pedido-na-sombra:{mid}"
        return replace(resposta, evento={**resposta.evento, "messageId": novo})

    def _gravar(self, novos: list[Registro]) -> None:
        if self._caminho is None:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._caminho)), exist_ok=True)
            with open(self._caminho, "a", encoding="utf-8") as f:
                f.writelines(json.dumps(r.como_json(), ensure_ascii=False) + "\n" for r in novos)
        except OSError as e:
            if not self._falha_avisada:
                self._falha_avisada = True
                print(f"  [sombra] falha ao gravar {self._caminho} (a coleta em memoria segue): {e}")

    def registros(self) -> list[Registro]:
        with self._lock:
            return list(self._registros)

    def limpar(self) -> None:
        with self._lock:
            self._registros.clear()
            self._ids.clear()


def caminho_padrao() -> str:
    return os.path.join(const_comum.DATA_DIR, const_analise.SOMBRA_ARQUIVO)


def ler_jsonl(caminho: str) -> list[Registro]:
    """Le o arquivo da sombra. Linha ilegivel (escrita cortada no meio) e pulada: o relatorio nao pode morrer por ela."""
    saida: list[Registro] = []
    if not os.path.exists(caminho):
        return saida
    with open(caminho, encoding="utf-8") as f:
        for linha in f:
            if not linha.strip():
                continue
            try:
                saida.append(Registro.de_json(json.loads(linha)))
            except (ValueError, KeyError, TypeError, AttributeError):    # JSON invalido ou que nao e um objeto
                continue
    return saida


# ============================================================================
# Comparacao
# ============================================================================
def tolerancia_instante(origem_a: str, origem_b: str) -> float:
    """Diferenca maxima (s) aceita entre o instante da decisao do caminho direto e o da sombra."""
    if ORIGEM_VIGIA in (origem_a, origem_b):
        return const_analise.INTERVALO_VIGIA_S + const_analise.ORCAMENTO_LATENCIA_S
    return const_analise.ORCAMENTO_LATENCIA_S


@dataclass(frozen=True)
class Divergencia:
    tipo: str                       # D_*
    registro: str                   # T_*
    user_id: int
    chave: str
    detalhe: str = ""
    direto: Registro | None = None
    sombra: Registro | None = None


@dataclass
class Comparacao:
    contagem: dict = field(default_factory=dict)        # T_* -> {"direto": n, "sombra": n, "pareados": n}
    divergencias: list[Divergencia] = field(default_factory=list)
    pendentes: int = 0                                  # do caminho direto, ainda dentro da graca da sombra
    reconstrucoes: int = 0                              # da sombra, nao comparaveis
    atrasos_s: list[float] = field(default_factory=list)

    @property
    def sem_divergencia(self) -> bool:
        return not self.divergencias

    @property
    def completa(self) -> bool:
        """Sem divergencia e sem nada pendente: o veredito e definitivo."""
        return not self.divergencias and self.pendentes == 0

    def atraso(self) -> dict:
        """Estatisticas do atraso da sombra (s): n, mediana, p95 (posto mais proximo) e maximo. Vazio se nao houve par."""
        xs = sorted(self.atrasos_s)
        if not xs:
            return {"n": 0, "mediana": None, "p95": None, "max": None}
        meio = len(xs) // 2
        mediana = xs[meio] if len(xs) % 2 else (xs[meio - 1] + xs[meio]) / 2
        return {"n": len(xs), "mediana": mediana, "p95": xs[max(math.ceil(0.95 * len(xs)) - 1, 0)], "max": xs[-1]}

    @property
    def atraso_dentro_do_orcamento(self) -> bool:
        p95 = self.atraso()["p95"]
        return p95 is None or p95 <= const_analise.ORCAMENTO_LATENCIA_S


def _colapsar_estados(registros: list[Registro]) -> list[Registro]:
    """Tira de cada (usuario, dispositivo) as repeticoes SEGUIDAS da mesma assinatura de estado: so a mudanca conta."""
    ultima: dict = {}
    saida = []
    for r in registros:
        if r.tipo != T_ESTADO:
            saida.append(r)
            continue
        chave_dispositivo = (r.user_id, json.loads(r.chave).get("device_id"))
        if ultima.get(chave_dispositivo) == r.chave:
            continue
        ultima[chave_dispositivo] = r.chave
        saida.append(r)
    return saida


def _agrupar(registros: Iterable[Registro]) -> dict:
    grupos: dict = {}
    for r in registros:
        grupos.setdefault((r.user_id, r.tipo, r.chave), []).append(r)
    return grupos


def comparar(registros: Iterable[Registro], agora: float | None = None) -> Comparacao:
    """Compara as decisoes do caminho direto com as da sombra. `agora` (relogio de parede, epoca em s) so serve para
    separar o que ainda pode chegar (decisao do caminho direto mais recente que a graca da sombra: `pendentes`) do que
    falta de verdade. Sem `agora` tudo e definitivo."""
    registros = list(registros)
    direto = _colapsar_estados([r for r in registros if r.lado == LADO_DIRETO])
    todas_sombra = [r for r in registros if r.lado == LADO_SOMBRA]
    sombra = _colapsar_estados([r for r in todas_sombra if not r.reconstrucao])

    res = Comparacao(reconstrucoes=sum(1 for r in todas_sombra if r.reconstrucao))
    res.contagem = {t: {"direto": 0, "sombra": 0, "pareados": 0} for t in TIPOS}
    for r in direto:
        res.contagem[r.tipo]["direto"] += 1
    for r in sombra:
        res.contagem[r.tipo]["sombra"] += 1

    gd, gs = _agrupar(direto), _agrupar(sombra)
    for (user_id, tipo, chave) in sorted(set(gd) | set(gs), key=lambda k: (k[0], k[1], k[2])):
        ds, ss = gd.get((user_id, tipo, chave), []), gs.get((user_id, tipo, chave), [])
        for d, s in zip(ds, ss):
            res.contagem[tipo]["pareados"] += 1
            _comparar_par(res, tipo, d, s)
        for d in ds[len(ss):]:
            graca = tolerancia_instante(d.origem, d.origem) + const_analise.ORCAMENTO_LATENCIA_S
            if agora is not None and d.visto_em is not None and agora - d.visto_em < graca:
                res.pendentes += 1
            else:
                res.divergencias.append(Divergencia(D_FALTANDO, tipo, user_id, chave, "o caminho direto decidiu e a sombra nao",
                                                    direto=d))
        for s in ss[len(ds):]:
            res.divergencias.append(Divergencia(D_A_MAIS, tipo, user_id, chave, "a sombra decidiu e o caminho direto nao",
                                                sombra=s))
    return res


def _comparar_par(res: Comparacao, tipo: str, d: Registro, s: Registro) -> None:
    if d.origem != s.origem:
        res.divergencias.append(Divergencia(D_ORIGEM, tipo, d.user_id, d.chave,
                                            f"direto por {d.origem}, sombra por {s.origem}", d, s))
    if d.instante is not None and s.instante is not None:
        dif = abs((s.instante - d.instante).total_seconds())
        tol = tolerancia_instante(d.origem, s.origem)
        if dif > tol:
            res.divergencias.append(Divergencia(D_INSTANTE, tipo, d.user_id, d.chave,
                                                f"diferenca de {dif:.3f} s (tolerancia {tol:.3f} s)", d, s))
    if (d.origem == s.origem and d.origem != ORIGEM_VIGIA and d.visto_em is not None and s.visto_em is not None):
        res.atrasos_s.append(s.visto_em - d.visto_em)


def filtrar_janela(registros: Iterable[Registro], inicio: float, fim: float) -> list[Registro]:
    """Registros cujo `visto_em` cai em [inicio, fim] (relogio de parede). Sem `visto_em` nao entra."""
    return [r for r in registros if r.visto_em is not None and inicio <= r.visto_em <= fim]


# ============================================================================
# Relatorio
# ============================================================================
@dataclass
class ResultadoCenario:
    nome: str
    descricao: str
    comparacao: Comparacao
    inicio: float | None = None
    fim: float | None = None


def _s(valor: float | None) -> str:
    return "-" if valor is None else f"{valor:.3f}"


def _veredito(c: Comparacao) -> str:
    if c.divergencias:
        return "DIVERGE"
    if c.pendentes:
        return "PENDENTE"
    if not c.atraso_dentro_do_orcamento:
        return "ATRASO"
    return "OK"


def _total(c: Comparacao, campo: str) -> int:
    return sum(v[campo] for v in c.contagem.values())


def renderizar_markdown(cenarios: list[ResultadoCenario], gerado_em: datetime | None = None, origem_dos_dados: str = "") -> str:
    """Relatorio de comparacao por cenario (Markdown). Arquive junto da linha de base da S0."""
    gerado_em = gerado_em or datetime.now(timezone.utc)
    linhas = ["# Relatório de comparação em modo sombra (S3b)", "",
              f"- Gerado em: {gerado_em.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"]
    if origem_dos_dados:
        linhas.append(f"- Dados: `{origem_dos_dados}`")
    linhas += [f"- Orçamento de latência (tolerância de instante e limite do atraso p95): {const_analise.ORCAMENTO_LATENCIA_S:.2f} s",
               f"- Tolerância de instante nas decisões do vigia: "
               f"{tolerancia_instante(ORIGEM_VIGIA, ORIGEM_VIGIA):.2f} s (periodo do vigia + orçamento)",
               f"- Decisões da sombra marcadas como reconstrução ficam fora da comparação (idade > {const_analise.IDADE_MAX_ACAO_S} s)",
               "", "## Resumo", "", "| Cenário | Direto | Sombra | Pareadas | Divergências | Atraso p95 (s) | Veredito |",
               "| --- | ---: | ---: | ---: | ---: | ---: | --- |"]
    for r in cenarios:
        c = r.comparacao
        linhas.append(f"| {r.nome} | {_total(c, 'direto')} | {_total(c, 'sombra')} | {_total(c, 'pareados')} | "
                      f"{len(c.divergencias)} | {_s(c.atraso()['p95'])} | **{_veredito(c)}** |")
    todos_ok = bool(cenarios) and all(_veredito(r.comparacao) == "OK" for r in cenarios)
    todos_atrasos = [a for r in cenarios for a in r.comparacao.atrasos_s]
    geral = Comparacao(atrasos_s=todos_atrasos)
    at = geral.atraso()
    linhas += ["", f"**Atraso da sombra em todos os cenários:** n={at['n']}, mediana={_s(at['mediana'])} s, "
                   f"p95={_s(at['p95'])} s, máximo={_s(at['max'])} s.", "",
               "## Critério de pronto da S3b", "",
               f"- [{'x' if todos_ok else ' '}] Em todos os cenários a sombra produz as mesmas decisões do caminho direto "
               "(sem decisão faltante e sem decisão a mais)",
               f"- [{'x' if at['n'] and geral.atraso_dentro_do_orcamento else ' '}] Atraso típico (p95) dentro do orçamento",
               "- [ ] Relatório arquivado junto da linha de base da S0 (manual)", ""]
    for r in cenarios:
        c = r.comparacao
        linhas += [f"## {r.nome}", ""]
        if r.descricao:
            linhas += [r.descricao, ""]
        linhas += ["| Tipo | Direto | Sombra | Pareadas |", "| --- | ---: | ---: | ---: |"]
        linhas += [f"| {t} | {v['direto']} | {v['sombra']} | {v['pareados']} |" for t, v in c.contagem.items()
                   if v["direto"] or v["sombra"]]
        a = c.atraso()
        linhas += ["", f"Atraso (s): n={a['n']}, mediana={_s(a['mediana'])}, p95={_s(a['p95'])}, máx={_s(a['max'])}."]
        if c.pendentes:
            linhas.append(f"Pendentes (a sombra ainda pode chegar): {c.pendentes}. Rode o relatório de novo.")
        if c.reconstrucoes:
            linhas.append(f"Decisões de reconstrução ignoradas: {c.reconstrucoes}.")
        if c.divergencias:
            linhas += ["", "Divergências:", ""]
            for d in c.divergencias[:20]:
                linhas.append(f"- `{d.tipo}` · {d.registro} · user {d.user_id} · {d.detalhe} · chave `{d.chave[:160]}`")
            if len(c.divergencias) > 20:
                linhas.append(f"- ... e mais {len(c.divergencias) - 20}")
        linhas.append("")
    return "\n".join(linhas)
