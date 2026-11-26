"""
teste_v2.py - Verificacao manual V2 (MUDANCA DE CONTEXTO: luz fria de dia x a noite) do projeto Flow.

A regra de luz (analise/regra_luz.py) alerta quando, A NOITE (18h as 6h) e com CCT acima de 4500 K, chegam 3 leituras
consecutivas. De dia, nada. IMPORTANTE: a regra decide "dia ou noite" pelo `event_time` DA PROPRIA LEITURA, e nao pelo relogio
do servidor. Por isso este script prova o V2 sem esperar anoitecer: manda a MESMA luz fria (CCT 6500 K) com event_time de
14h (dia) e depois de 22h (noite).

  Caso DIA   : 4 leituras com CCT 6500 K e event_time 14:00  -> NENHUM alerta de luz
  Caso NOITE : 4 leituras com CCT 6500 K e event_time 22:00  -> UM alerta de luz (na 3a leitura), visivel ao cuidador

Faz o papel do app (conta idoso1) e confere como o cuidador (conta cuidador1) pelo historico.

Preparacao: banco, borda E ANALISE no ar; contas idoso1 e cuidador1 do mesmo user_id.
Como rodar (em SensorServer/, junto com v_comum.py):
    $env:FLOW_IDOSO_SECRET="<senha do idoso1>"; $env:FLOW_CUIDADOR_SECRET="<senha do cuidador1>"
    python teste_v2.py
Cada execucao usa um device_id novo (a regra guarda estado por dispositivo), entao pode ser repetida.
"""
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import v_comum as c

FUSO = timezone(timedelta(hours=-3))                 # o mesmo do seu relogio (America/Buenos_Aires); so a HORA importa
CCT_FRIO = 6500                                      # acima do limiar de 4500 K
DEVICE_APP = f"script-v2-app-{int(time.time())}"     # aparelho "novo" a cada execucao: estado da regra limpo
DEVICE_CUIDADOR = "script-v2-cuidador"
ESPERA_ANALISE_S = 4                                 # a borda grava em lote e chama a analise a cada 0,25 s


def main() -> None:
    tok_app = c.login("idoso1", c.senha("FLOW_IDOSO_SECRET", "Senha de idoso1: "), "app", DEVICE_APP)
    tok_cuid = c.login("cuidador1", c.senha("FLOW_CUIDADOR_SECRET", "Senha de cuidador1: "), "app", DEVICE_CUIDADOR)
    user_id = tok_app["user_ids"][0]
    cab_app = {"Authorization": f"Bearer {tok_app['access_token']}"}
    seq = int(time.time())                           # seq_num crescente (a regra descarta repetidos)

    with tempfile.TemporaryDirectory(prefix="flow_v2_") as pasta:
        cert_app = c.pedir_certificado(tok_app["access_token"], DEVICE_APP, Path(pasta), "app")
        cert_cuid = c.pedir_certificado(tok_cuid["access_token"], DEVICE_CUIDADOR, Path(pasta), "cuid")

        def alertas_de_luz(desde: datetime) -> list[dict]:
            r = c.cuidador_le("historico", user_id, tok_cuid["access_token"], cert_cuid)
            r.raise_for_status()
            return [a for a in c.decifrar(r)["alertas"]
                    if "luz" in a["tipo"].lower() and a["deviceId"] == DEVICE_APP
                    and datetime.fromisoformat(a["instante"]) >= desde]

        def enviar(hora: int) -> int:
            """Uma leitura do app: acelerometro em repouso + leitura_ambiente com luz fria no horario `hora` (event_time)."""
            nonlocal seq
            seq += 1
            agora = datetime.now(FUSO)
            event_time = agora.replace(hour=hora, minute=0, second=seq % 60, microsecond=0)
            corpo = {
                "schemaVersion": c.const_comum.SCHEMA_VERSION_APP, "source": "app", "user_id": user_id,
                "device_id": DEVICE_APP, "timestamp_envio": int(time.time() * 1000),
                "acelerometro": {"magnitude": 9.81, "magnitude_max_janela": 9.9, "amostras": 10, "janela_ms": 1000},
                "leitura_ambiente": {"schemaVersion": c.const_comum.SCHEMA_VERSION_APP, "device_id": DEVICE_APP,
                                     "event_time": event_time.isoformat(), "luminosidade": 120.0, "cct": CCT_FRIO,
                                     "seq_num": seq},
            }
            return requests.post(f"{c.BASE}/dados", json=c.env(corpo), headers=cab_app, verify=c.CA, cert=cert_app,
                                 timeout=15).status_code

        resultados = {}
        for nome, hora, esperado in (("DIA (event_time 14:00)", 14, 0), ("NOITE (event_time 22:00)", 22, 1)):
            print(f"\n=== Caso {nome}: 4 leituras com CCT {CCT_FRIO} K ===")
            inicio = datetime.now(timezone.utc)
            codigos = []
            for _ in range(4):
                codigos.append(enviar(hora))
                time.sleep(1)
            print(f"HTTP das leituras: {codigos}")
            time.sleep(ESPERA_ANALISE_S)
            alertas = alertas_de_luz(inicio)
            for a in alertas:
                print(f"  alerta: {a['tipo']} | {a['mensagem']}")
            resultados[nome] = len(alertas) == esperado
            print(f"alertas de luz vistos pelo cuidador: {len(alertas)} (esperado {esperado})  ->  "
                  f"{'OK' if resultados[nome] else 'DIFERENTE'}")

    print("\n=== Resumo V2 ===")
    for nome, ok in resultados.items():
        print(f"{nome:26} {'OK' if ok else 'DIFERENTE'}")
    print("Evidencia: esta saida e a tela do historico do cuidador no app (alerta de luz).")


if __name__ == "__main__":
    main()
