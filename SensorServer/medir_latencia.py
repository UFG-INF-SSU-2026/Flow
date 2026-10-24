"""
Mede, ponta a ponta, quanto o servidor leva para empurrar o pedido de confirmacao depois do gatilho.
E a REGUA do orcamento de latencia: rode ANTES de uma mudanca que mexa no caminho dos dados (linha de base) e depois
dela.

Faz login como o bridge (conta 'dispositivo', fonte wokwi), abre o SSE /push/wokwi e repete N rodadas de:
  - queda:        2 leituras normais, IMPACTO, repouso. Tempo = envio do impacto ate o push chegar.
  - imobilidade:  1 decisao ALERTA_IMOBILIDADE (gatilho B). Tempo = envio da decisao ate o push chegar.
Entre rodadas espera o episodio expirar e o cooldown passar (const_analise.COOLDOWN_S): por isso cada rodada leva ~1 min.

Com a linha de base (as medianas medidas ANTES da mudanca) o script diz se a mudanca coube no orcamento de latencia
(const_analise.ORCAMENTO_LATENCIA_S, 3 esperas de 250 ms). Informe as medianas da linha de base em segundos:
    FLOW_LATENCIA_BASE_QUEDA_S=<mediana da queda>  FLOW_LATENCIA_BASE_IMOB_S=<mediana da imobilidade>
O veredito compara o PIOR CASO medido com a base mais o orcamento. Fora do orcamento: vale avaliar
LISTEN/NOTIFY, so como complemento ao cursor. Sem a base, o script so imprime os tempos.

Uso (na pasta SensorServer, com o servidor rodando; a borda e a analise sobem por `python -m borda` e `python -m analise`):
    FLOW_BRIDGE_ID=bridge1 FLOW_BRIDGE_SECRET=<senha> python medir_latencia.py [URL_BASE] [RODADAS]
"""
import json
import os
import statistics
import sys
import threading
import time

import requests

from comum import const as const_comum
from analise import const as const_analise
from borda.crypto_utils import decrypt_payload, encrypt_payload

# Nada aqui faz rede nem le argv/ambiente ao importar (o test_const importa todo .py da raiz): tudo isso ocorre em main().
BASE = "http://localhost:5000"
CONTA = os.environ.get("FLOW_BRIDGE_ID", "bridge1")
DEVICE = "medidor-latencia"
TIPO_PEDIDO = "queda.confirmacao_solicitada"


def env(obj):
    n, c = encrypt_payload(json.dumps(obj))
    return {"nonce": n, "ciphertext": c}


def login():
    r = requests.post(f"{BASE}/auth/login", json=env({"schemaVersion": const_comum.SCHEMA_VERSION_AUTH, "identificador": CONTA,
                                                      "segredo": os.environ["FLOW_BRIDGE_SECRET"], "fonte": "wokwi", "device_id": DEVICE}), timeout=10)
    r.raise_for_status()
    j = r.json()
    return json.loads(decrypt_payload(j["nonce"], j["ciphertext"]))


H: dict = {}
USER: int | None = None
_seq = int(time.time())
_chegadas: list[tuple[float, str]] = []     # (instante monotonico, messageType)
_lock = threading.Lock()


def ouvir_sse():
    with requests.get(f"{BASE}/push/wokwi", headers=H, stream=True, timeout=(5, None)) as r:
        r.raise_for_status()
        evento = None
        for linha in r.iter_lines(decode_unicode=True):
            if linha.startswith("event:"):
                evento = linha[6:].strip()
            elif linha.startswith("data:") and evento == "push":
                t = time.monotonic()
                env_ = json.loads(linha[5:].strip())
                msg = json.loads(decrypt_payload(env_["nonce"], env_["ciphertext"]))
                with _lock:
                    _chegadas.append((t, msg["messageType"]))


def enviar(tipo, state, value, value_max=None):
    global _seq
    _seq += 1
    ev = {"schemaVersion": const_comum.SCHEMA_VERSION_WOKWI, "eventType": tipo, "deviceId": DEVICE, "entityId": "medidor",
          "eventTimeMs": _seq * 1000, "sequence": _seq, "value": value, "unit": "m/s2", "state": state,
          "user_id": USER, "source": "wokwi"}
    if value_max is not None:
        ev["valueMax"] = value_max
    t = time.monotonic()
    requests.post(f"{BASE}/dados", json=env(ev), headers=H, timeout=10).raise_for_status()
    return t


def esperar_pedido(desde: float, limite_s: float = 15.0) -> float | None:
    fim = time.monotonic() + limite_s
    while time.monotonic() < fim:
        with _lock:
            for t, tipo in _chegadas:
                if tipo == TIPO_PEDIDO and t >= desde:
                    return t
        time.sleep(0.01)
    return None


def leitura(v, vmax):
    enviar("imobilidade.leitura", "NORMAL", v, vmax)


def rodada_queda() -> float | None:
    for _ in range(2):
        leitura(1.0, 1.5); time.sleep(1)
    t0 = enviar("imobilidade.leitura", "NORMAL", 3.0, 25.0)      # impacto
    for _ in range(6):                                              # repouso (o gatilho A dispara apos T_POS_IMPACTO_S)
        time.sleep(1); leitura(0.3, 0.4)
    t = esperar_pedido(t0)
    return None if t is None else t - t0


def rodada_imobilidade() -> float | None:
    t0 = enviar("imobilidade.decisao", "ALERTA_IMOBILIDADE", 0.3)
    t = esperar_pedido(t0)
    return None if t is None else t - t0


def esperar_cooldown():
    espera = const_comum.TIMEOUT_CONFIRMACAO_S + const_comum.MARGEM_SERVIDOR_S + const_analise.COOLDOWN_S + 3
    print(f"  aguardando {espera}s (episodio expira + cooldown)...")
    time.sleep(espera)


def resumo(nome, medidas):
    ok = [m for m in medidas if m is not None]
    print(f"\n{nome}: {len(ok)}/{len(medidas)} rodadas com push")
    if ok:
        print(f"  tempos (s): {', '.join(f'{m:.2f}' for m in ok)}")
        print(f"  min {min(ok):.2f}  mediana {statistics.median(ok):.2f}  max {max(ok):.2f}")


def base_da_s0(nome: str) -> float | None:
    """Mediana da linha de base (variavel de ambiente `nome`, em segundos), ou None se nao informada."""
    bruto = os.environ.get(nome)
    if not bruto:
        return None
    try:
        return float(bruto.replace(",", "."))
    except ValueError:
        raise SystemExit(f"{nome} invalida: {bruto!r} (esperado um numero em segundos)")


def veredito(nome, medidas, base):
    """Compara o pior caso medido com `base` + orcamento de latencia. Imprime e devolve True (cabe),
    False (nao cabe) ou None (sem base ou sem medida)."""
    ok = [m for m in medidas if m is not None]
    if base is None or not ok:
        return None
    orcamento = const_analise.ORCAMENTO_LATENCIA_S
    acrescimo_mediano, acrescimo_pior = statistics.median(ok) - base, max(ok) - base
    cabe = acrescimo_pior <= orcamento
    print(f"  {nome}: base {base:.2f}s; acrescimo mediano {acrescimo_mediano:+.2f}s, pior caso {acrescimo_pior:+.2f}s "
          f"(orcamento {orcamento:.2f}s) -> {'DENTRO do orcamento' if cabe else 'FORA do orcamento'}")
    return cabe


def main():
    global BASE, RODADAS, H, USER
    BASE = (sys.argv[1] if len(sys.argv) > 1 else BASE).rstrip("/")
    RODADAS = int(sys.argv[2]) if len(sys.argv) > 2 else RODADAS
    tok = login()
    H = {"Authorization": f"Bearer {tok['access_token']}"}
    USER = tok["user_ids"][0]
    threading.Thread(target=ouvir_sse, daemon=True).start()
    time.sleep(1)
    print(f"Medindo latencia em {BASE} (user_id={USER}, {RODADAS} rodada(s) por cenario)")
    queda, imob = [], []
    for i in range(RODADAS):
        print(f"\nrodada {i + 1}/{RODADAS}: queda")
        queda.append(rodada_queda()); print(f"  -> {queda[-1]}")
        esperar_cooldown()
        print(f"rodada {i + 1}/{RODADAS}: imobilidade")
        imob.append(rodada_imobilidade()); print(f"  -> {imob[-1]}")
        if i + 1 < RODADAS:
            esperar_cooldown()
    resumo("queda (impacto -> push; inclui os 3 s de imobilidade exigidos)", queda)
    resumo("imobilidade (decisao -> push)", imob)
    vereditos = [veredito("queda", queda, base_da_s0("FLOW_LATENCIA_BASE_QUEDA_S")),
                 veredito("imobilidade", imob, base_da_s0("FLOW_LATENCIA_BASE_IMOB_S"))]
    if any(v is False for v in vereditos):
        print("\nA mudanca nao coube no orcamento: avalie LISTEN/NOTIFY como COMPLEMENTO ao cursor (nunca no lugar dele).")
    elif all(v is None for v in vereditos):
        print("\nSem a linha de base nao ha veredito (FLOW_LATENCIA_BASE_QUEDA_S e FLOW_LATENCIA_BASE_IMOB_S).")
    print("\nAnote estes numeros: eles sao a linha de base da proxima comparacao.")


if __name__ == "__main__":
    main()