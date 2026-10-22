"""
S3b do split: relatorio da comparacao em modo sombra (caminho direto x laco da analise), por cenario.

Le dataUsers/sombra_decisoes.jsonl (escrito pelo servidor com FLOW_SOMBRA=1) e, se existir,
dataUsers/sombra_cenarios.json (janelas de tempo escritas por simular_cenarios.py). Com o manifesto, separa as decisoes
por cenario; sem ele, compara tudo junto (uso real). Imprime o resumo, grava o relatorio em Markdown e sai com 0 se
TODOS os cenarios estao OK (mesmas decisoes, sem pendentes, atraso p95 dentro do orcamento) e 1 se nao.

Uso (na pasta SensorServer):
    python relatorio_sombra.py [--arquivo CAMINHO] [--manifesto CAMINHO] [--saida CAMINHO.md] [--desde EPOCA]

`--desde` ignora decisoes mais antigas que a epoca dada (segundos): util para uso real, depois de um reinicio.
Arquive o relatorio junto da linha de base da S0 (por padrao: docs/split-sombra-relatorio.md).
Nada aqui le argv nem faz I/O ao importar.
"""
import json
import os
import sys
import time

from analise import const as const_analise
from analise import sombra
from comum import const as const_comum

SAIDA_PADRAO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "split-sombra-relatorio.md")
MARGEM_FIM_S = 0.5      # a janela de cada cenario vai um pouco alem do fim anotado


def _argumentos(argv: list[str]) -> dict:
    opcoes = {"arquivo": sombra.caminho_padrao(),
              "manifesto": os.path.join(const_comum.DATA_DIR, const_analise.SOMBRA_MANIFESTO),
              "saida": SAIDA_PADRAO, "desde": None}
    it = iter(argv)
    for a in it:
        nome = a[2:] if a.startswith("--") else None
        if nome in opcoes:
            valor = next(it, None)
            if valor is None:
                raise SystemExit(f"falta o valor de {a}")
            opcoes[nome] = float(valor) if nome == "desde" else valor
        else:
            raise SystemExit(f"argumento desconhecido: {a}")
    return opcoes


def montar(registros: list[sombra.Registro], manifesto: dict | None, agora: float) -> list[sombra.ResultadoCenario]:
    """Um `ResultadoCenario` por cenario do manifesto (ou um so, "uso real", se nao ha manifesto)."""
    if not manifesto or not manifesto.get("cenarios"):
        return [sombra.ResultadoCenario("uso real (todas as decisoes)", "Sem manifesto de cenarios: tudo junto.",
                                        sombra.comparar(registros, agora))]
    saida = []
    for c in manifesto["cenarios"]:
        janela = sombra.filtrar_janela(registros, c["inicio"], c["fim"] + MARGEM_FIM_S)
        saida.append(sombra.ResultadoCenario(c["nome"], c.get("descricao", ""), sombra.comparar(janela, agora),
                                             c["inicio"], c["fim"]))
    return saida


def main(argv: list[str] | None = None) -> int:
    op = _argumentos(sys.argv[1:] if argv is None else argv)
    registros = sombra.ler_jsonl(op["arquivo"])
    if op["desde"] is not None:
        registros = [r for r in registros if r.visto_em is not None and r.visto_em >= op["desde"]]
    if not registros:
        print(f"Nenhuma decisao em {op['arquivo']}. O servidor esta com FLOW_SOMBRA=1 e o laco ligado? "
              "Rode os cenarios (simular_cenarios.py) ou use o sistema antes.")
        return 2
    manifesto = None
    if os.path.exists(op["manifesto"]):
        with open(op["manifesto"], encoding="utf-8") as f:
            manifesto = json.load(f)
    resultados = montar(registros, manifesto, time.time())
    try:
        origem = os.path.relpath(op["arquivo"])
    except ValueError:      # Windows: outro disco
        origem = op["arquivo"]
    texto = sombra.renderizar_markdown(resultados, origem_dos_dados=origem)
    os.makedirs(os.path.dirname(os.path.abspath(op["saida"])), exist_ok=True)
    with open(op["saida"], "w", encoding="utf-8") as f:
        f.write(texto + "\n")

    print(f"{'cenario':28s} {'direto':>7s} {'sombra':>7s} {'diverg.':>8s} {'p95 (s)':>8s}  veredito")
    for r in resultados:
        c = r.comparacao
        total = lambda campo: sum(v[campo] for v in c.contagem.values())   # noqa: E731
        p95 = c.atraso()["p95"]
        veredito = ("DIVERGE" if c.divergencias else "PENDENTE" if c.pendentes
                    else "ATRASO" if not c.atraso_dentro_do_orcamento else "OK")
        print(f"{r.nome[:28]:28s} {total('direto'):7d} {total('sombra'):7d} {len(c.divergencias):8d} "
              f"{'-' if p95 is None else format(p95, '.3f'):>8s}  {veredito}")
    print(f"\nRelatorio: {os.path.abspath(op['saida'])}")
    return 0 if all(r.comparacao.completa and r.comparacao.atraso_dentro_do_orcamento for r in resultados) else 1


if __name__ == "__main__":
    sys.exit(main())
