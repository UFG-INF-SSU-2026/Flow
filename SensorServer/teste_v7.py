"""
teste_v7.py - Verificacao manual V7 (falha e recuperacao da ANALISE) do projeto Flow.

O que o V7 verifica (docs/testes.md):
    Com a analise parada e dados chegando, a borda deve
      (a) seguir ACEITANDO e GRAVANDO as leituras no banco;
      (b) acusar "degradado" em GET /health, com a fila de espera da analise crescendo;
    e, quando a analise volta,
      (c) voltar para "ok" em /health e
      (d) PROCESSAR a fila (a fila de espera da analise esvazia).

Como o script ajuda: ele simula o ESP32 (envia leituras de repouso 1x/s, como o firmware), le /health e confere no banco
(via GET /dados do admin) se as leituras foram gravadas. Parar e subir a ANALISE e com voce (e um processo separado): o
script pausa e avisa na hora certa.

Preparacao:
    - Banco, borda e analise no ar (scripts/tudo.sh ou tudo.ps1). Pare o Wokwi de verdade: este script usa o mesmo device_id
      e o dois enviando juntos misturariam as leituras (e a deduplicacao por "sequence" descartaria parte delas).
    - Conta do dispositivo (bridge1) e a do admin (admin1) existentes.

Como rodar (na pasta SensorServer/):
    pip install requests
    $env:FLOW_BRIDGE_SECRET="<senha do bridge1>"; $env:FLOW_ADMIN_SECRET="<senha do admin1>"
    python teste_v7.py                  # padrao: 20 s de envio com a analise parada
    python teste_v7.py 40               # 40 s de envio
Se as variaveis de senha nao estiverem definidas, o script pergunta no terminal (sem mostrar o que voce digita).

Ele fala direto com a BORDA (https://127.0.0.1:5000): o que o V7 testa e a borda e a analise, nao o bridge.
Outra URL: FLOW_BORDA_URL.
"""
import getpass
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

# Imports do proprio projeto: por isso o script roda DENTRO de SensorServer/.
from borda.crypto_utils import decrypt_payload, encrypt_payload
from comum import const as const_comum
from comum import tls

# ---------------------------------------------------------------------------------------------------------------------
# CONFIGURACAO
# ---------------------------------------------------------------------------------------------------------------------
BASE = os.environ.get("FLOW_BORDA_URL", "https://127.0.0.1:5000").rstrip("/")
CA = tls.ca()                                    # certificado da CA do projeto: a verificacao TLS NUNCA e desligada aqui
CONTA_DISPOSITIVO = os.environ.get("FLOW_BRIDGE_ID", "bridge1")
CONTA_ADMIN = os.environ.get("FLOW_ADMIN_ID", "admin1")
DEVICE_ID = os.environ.get("FLOW_DEVICE_ID", "esp32-decisao-imobilidade-01")
DURACAO_S = int(sys.argv[1]) if len(sys.argv) > 1 else 20      # quantos segundos enviar com a analise PARADA
LIMITE_GET_DADOS = 200                                         # teto de GET /dados (borda/const.py): limita o que da p/ conferir
ESPERA_DEGRADADO_S = 30                                        # quanto esperar o /health mudar para "degradado"
ESPERA_RECUPERACAO_S = 90                                      # quanto esperar a analise voltar e a fila esvaziar


# ---------------------------------------------------------------------------------------------------------------------
# FUNCOES AUXILIARES
# ---------------------------------------------------------------------------------------------------------------------
def env(obj: dict) -> dict:
    """Cifra o JSON no ENVELOPE (AES-256-GCM, chave compartilhada) que a borda exige em login, certificado e dados."""
    nonce, texto_cifrado = encrypt_payload(json.dumps(obj))
    return {"nonce": nonce, "ciphertext": texto_cifrado}


def decifrar(resposta: requests.Response) -> dict:
    envelope = resposta.json()
    return json.loads(decrypt_payload(envelope["nonce"], envelope["ciphertext"]))


def senha(variavel: str, pergunta: str) -> str:
    return os.environ.get(variavel) or getpass.getpass(pergunta)


def login(identificador: str, segredo: str, fonte: str | None, device_id: str | None) -> dict:
    """POST /auth/login. O dispositivo entra com fonte "wokwi" e o device_id; o admin, sem fonte nem device_id."""
    r = requests.post(f"{BASE}/auth/login", verify=CA, timeout=15, json=env({
        "schemaVersion": const_comum.SCHEMA_VERSION_AUTH, "identificador": identificador, "segredo": segredo,
        "fonte": fonte, "device_id": device_id}))
    if r.status_code != 200:
        sys.exit(f"Login de {identificador} falhou: HTTP {r.status_code} {r.text.strip()}")
    return decifrar(r)


def pedir_certificado(token: str, pasta: Path) -> tuple[str, str]:
    """O dispositivo (wokwi) so pode enviar /dados com um certificado de cliente, que ele mesmo pede: gera uma chave EC
    P-256 + CSR, a borda assina, e os dois vao para arquivos temporarios (o requests so aceita certificado por arquivo)."""
    chave = ec.generate_private_key(ec.SECP256R1())
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, DEVICE_ID)]))
           .sign(chave, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode("ascii"))
    r = requests.post(f"{BASE}/auth/certificado", json=env({"schemaVersion": 1, "csr": csr}),
                      headers={"Authorization": f"Bearer {token}"}, verify=CA, timeout=15)
    r.raise_for_status()
    cert, arq_chave = pasta / "cliente.crt", pasta / "cliente.key"
    cert.write_text(decifrar(r)["certificado"], encoding="ascii")
    arq_chave.touch(mode=0o600)
    arq_chave.write_bytes(chave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
    return str(cert), str(arq_chave)


def health() -> dict:
    """GET /health (sem token): a borda responde 200 mesmo com a analise parada; o que muda e analise.estado."""
    r = requests.get(f"{BASE}/health", verify=CA, timeout=10)
    return r.json()


def resumo(h: dict) -> str:
    a = h["analise"]
    return (f"analise={a['estado']:9} fila_analise={a['fila']:4} fila_leituras={h['borda']['fila_leituras']:3} "
            f"ultima_resposta_ha_s={a['ultima_resposta_ha_s']}")


def esperar_enter(mensagem: str) -> None:
    print(f"\n>>> {mensagem}")
    input("    Pressione ENTER quando fizer isso... ")


def esperar_ate(condicao, limite_s: int, passo_s: float = 1.0) -> bool:
    """Repete `condicao()` ate ser verdadeira ou o prazo acabar, mostrando o /health a cada passo."""
    fim = time.monotonic() + limite_s
    while time.monotonic() < fim:
        h = health()
        print("    " + resumo(h))
        if condicao(h):
            return True
        time.sleep(passo_s)
    return False


def leituras_gravadas_desde(token_admin: str, user_id: int, instante: datetime) -> int:
    """Conta, via GET /dados (so admin), as leituras do dispositivo gravadas no banco DEPOIS de `instante`."""
    r = requests.get(f"{BASE}/dados", params={"user_id": user_id, "limite": LIMITE_GET_DADOS},
                     headers={"Authorization": f"Bearer {token_admin}"}, verify=CA, timeout=15)
    r.raise_for_status()
    return sum(1 for l in r.json()
               if l["device_id"] == DEVICE_ID and datetime.fromisoformat(l["timestamp"]) >= instante)


# ---------------------------------------------------------------------------------------------------------------------
# ROTEIRO DO V7
# ---------------------------------------------------------------------------------------------------------------------
def main() -> None:
    if DURACAO_S > LIMITE_GET_DADOS:
        sys.exit(f"A duracao maxima e {LIMITE_GET_DADOS} s (limite do GET /dados para conferir o que foi gravado).")

    print("== Preparacao ==")
    tok_disp = login(CONTA_DISPOSITIVO, senha("FLOW_BRIDGE_SECRET", f"Senha de {CONTA_DISPOSITIVO}: "),
                     "wokwi", DEVICE_ID)
    tok_admin = login(CONTA_ADMIN, senha("FLOW_ADMIN_SECRET", f"Senha de {CONTA_ADMIN}: "), None, None)
    cab_disp = {"Authorization": f"Bearer {tok_disp['access_token']}"}
    user_id = tok_disp["user_ids"][0]
    h0 = health()
    print("/health antes de comecar:   " + resumo(h0))
    if h0["analise"]["estado"] != "ok":
        sys.exit("A analise ja esta 'degradado': suba-a (scripts/analise.sh ou tudo) e rode de novo, para partir de 'ok'.")

    seq = int(time.time())          # sequence crescente; eventTimeMs acompanha (como o firmware, em ms)

    with tempfile.TemporaryDirectory(prefix="flow_v7_") as pasta:
        cert = pedir_certificado(tok_disp["access_token"], Path(pasta))

        def enviar_repouso() -> int:
            """Uma leitura de repouso (value ~0, abaixo do limiar): NAO dispara queda nem alerta; so alimenta a fila."""
            nonlocal seq
            seq += 1
            ev = {"schemaVersion": const_comum.SCHEMA_VERSION_WOKWI, "eventType": "imobilidade.leitura",
                  "deviceId": DEVICE_ID, "entityId": "idoso-simulado", "eventTimeMs": seq * 1000, "sequence": seq,
                  "value": 0.3, "valueMax": 0.4, "unit": "m/s2", "state": "NORMAL",
                  "user_id": user_id, "source": "wokwi"}
            return requests.post(f"{BASE}/dados", json=env(ev), headers=cab_disp, verify=CA, cert=cert,
                                 timeout=15).status_code

        # PASSO 1: a analise cai
        esperar_enter("PARE A ANALISE agora (Ctrl+C no terminal dela; deixe borda e banco rodando).")
        print("Esperando o /health acusar 'degradado' (o vigia da borda chama a analise a cada instante)...")
        if not esperar_ate(lambda h: h["analise"]["estado"] == "degradado", ESPERA_DEGRADADO_S):
            sys.exit("O /health nao ficou 'degradado'. A analise foi mesmo parada?")
        print("-> (b) CONFIRMADO: /health = degradado")

        # PASSO 2: dados chegando com a analise fora
        instante_inicial = datetime.now(timezone.utc)
        print(f"\n== Enviando leituras por {DURACAO_S} s com a analise PARADA ==")
        aceitas = 0
        for i in range(DURACAO_S):
            codigo = enviar_repouso()
            aceitas += codigo == 200
            print(f"  leitura {i + 1:3}: HTTP {codigo}   " + resumo(health()))
            time.sleep(1)
        time.sleep(1)       # a descarga em lote roda a cada 0,25 s; 1 s e folga para a ultima chegar ao banco
        gravadas = leituras_gravadas_desde(tok_admin["access_token"], user_id, instante_inicial)
        fila_com_analise_parada = health()["analise"]["fila"]
        print(f"\nEnviadas aceitas (HTTP 200): {aceitas}/{DURACAO_S}")
        print(f"Gravadas no banco (GET /dados, desde o inicio do envio): {gravadas}")
        print(f"Fila de espera da analise agora: {fila_com_analise_parada}")
        ok_a = aceitas == DURACAO_S and gravadas >= aceitas
        print("-> (a) " + ("CONFIRMADO: a borda aceitou e gravou todas as leituras com a analise fora"
                           if ok_a else "DIFERENTE: faltaram leituras aceitas ou gravadas"))

        # PASSO 3: a analise volta
        esperar_enter("SUBA A ANALISE de novo (scripts/analise.sh ou: python -m analise).")
        print("Esperando a analise voltar ('ok') e a fila esvaziar...")
        voltou = esperar_ate(lambda h: h["analise"]["estado"] == "ok" and h["analise"]["fila"] == 0,
                             ESPERA_RECUPERACAO_S)
        ok_c = voltou
        print("-> (c)+(d) " + ("CONFIRMADO: /health = ok e a fila foi processada (esvaziou)"
                               if ok_c else "DIFERENTE: a analise nao voltou a 'ok' ou a fila nao esvaziou no prazo"))

    print("\n== Resumo do V7 ==")
    print(f"(a) leituras gravadas com a analise fora : {'OK' if ok_a else 'DIFERENTE'}  ({gravadas}/{aceitas})")
    print(f"(b) /health acusou 'degradado'            : OK  (fila chegou a {fila_com_analise_parada})")
    print(f"(c)(d) voltou a 'ok' e a fila foi processada: {'OK' if ok_c else 'DIFERENTE'}")
    print("Guarde esta saida e a do terminal da borda ('[pipeline] analise fora do ar') como evidencia.")


if __name__ == "__main__":
    main()