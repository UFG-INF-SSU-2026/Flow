"""
teste_v9.py - Verificacao manual V9 (falha e recuperacao: SEM RESPOSTA) do projeto Flow.

O que o V9 verifica (docs/testes.md):
    Abre-se um episodio de queda e NINGUEM toca em "Estou bem". No fim do prazo o episodio deve fechar como EXPIRADO,
    gravado no banco e VISIVEL AO CUIDADOR (GET /cuidador/<user_id>/historico).

O que o script faz:
    1. Simula o ESP32 (conta bridge1): leituras de repouso, um IMPACTO e repouso de novo. Isso faz a analise concluir
       "queda provavel" (impacto seguido de imobilidade) e abrir o episodio. Ninguem responde ao pedido.
    2. Entra como CUIDADOR (conta cuidador1) e consulta, a cada segundo, o estado do episodio:
       aberto ("aguardando_confirmacao") -> encerrado, com resultado EXPIRADO.
    3. Mede quanto tempo o episodio levou entre abrir e expirar. O esperado e proximo de TIMEOUT_CONFIRMACAO_S (5 s) +
       MARGEM_SERVIDOR_S (2 s) = 7 s (comum/const.py).

Preparacao:
    - Banco, borda e analise NO AR (a analise e quem decide a queda). Wokwi de verdade e app: PARADOS ou sem tocar no
      "Estou bem" (se um deles responder, o episodio fecha como CONFIRMADO, e nao EXPIRADO).
    - Contas existentes: bridge1 (dispositivo, user_id 1) e cuidador1 (cuidador, user_id 1).
    - Espere pelo menos 10 s desde o ultimo episodio (COOLDOWN_S): dentro desse intervalo nenhum episodio novo abre.

Como rodar (na pasta SensorServer/):
    pip install requests
    $env:FLOW_BRIDGE_SECRET="<senha do bridge1>"; $env:FLOW_CUIDADOR_SECRET="<senha do cuidador1>"
    python teste_v9.py
Sem as variaveis, o script pergunta as senhas no terminal (sem mostrar). Fala direto com a BORDA (https://127.0.0.1:5000;
outra URL em FLOW_BORDA_URL).
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
CA = tls.ca()                                        # CA do projeto: a verificacao TLS nunca e desligada
CONTA_DISPOSITIVO = os.environ.get("FLOW_BRIDGE_ID", "bridge1")
CONTA_CUIDADOR = os.environ.get("FLOW_CUIDADOR_ID", "cuidador1")
DEVICE_ID = os.environ.get("FLOW_DEVICE_ID", "esp32-decisao-imobilidade-01")     # o do sketch.ino
DEVICE_ID_CUIDADOR = "script-v9-cuidador"            # o cuidador entra "como um celular": qualquer nome de aparelho serve
ESPERA_ABRIR_S = 20                                  # quanto esperar o episodio abrir depois do impacto
ESPERA_EXPIRAR_S = 25                                # quanto esperar o episodio fechar depois de aberto
PRAZO_ESPERADO_S = const_comum.TIMEOUT_CONFIRMACAO_S + const_comum.MARGEM_SERVIDOR_S      # 5 + 2 = 7 s


# ---------------------------------------------------------------------------------------------------------------------
# AUXILIARES
# ---------------------------------------------------------------------------------------------------------------------
def env(obj: dict) -> dict:
    """Cifra o JSON no ENVELOPE (AES-256-GCM) que a borda exige em login, certificado e dados."""
    nonce, texto_cifrado = encrypt_payload(json.dumps(obj))
    return {"nonce": nonce, "ciphertext": texto_cifrado}


def decifrar(resposta: requests.Response) -> dict:
    envelope = resposta.json()
    return json.loads(decrypt_payload(envelope["nonce"], envelope["ciphertext"]))


def senha(variavel: str, pergunta: str) -> str:
    return os.environ.get(variavel) or getpass.getpass(pergunta)


def login(identificador: str, segredo: str, fonte: str, device_id: str) -> dict:
    r = requests.post(f"{BASE}/auth/login", verify=CA, timeout=15, json=env({
        "schemaVersion": const_comum.SCHEMA_VERSION_AUTH, "identificador": identificador, "segredo": segredo,
        "fonte": fonte, "device_id": device_id}))
    if r.status_code != 200:
        sys.exit(f"Login de {identificador} falhou: HTTP {r.status_code} {r.text.strip()}")
    return decifrar(r)


def pedir_certificado(token: str, device_id: str, pasta: Path, prefixo: str) -> tuple[str, str]:
    """Gera chave EC P-256 + CSR (CN = device_id), a borda o assina; chave e certificado vao para arquivos temporarios
    (o requests so aceita certificado de cliente por arquivo), apagados ao fim."""
    chave = ec.generate_private_key(ec.SECP256R1())
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, device_id)]))
           .sign(chave, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode("ascii"))
    r = requests.post(f"{BASE}/auth/certificado", json=env({"schemaVersion": 1, "csr": csr}),
                      headers={"Authorization": f"Bearer {token}"}, verify=CA, timeout=15)
    r.raise_for_status()
    cert, arq_chave = pasta / f"{prefixo}.crt", pasta / f"{prefixo}.key"
    cert.write_text(decifrar(r)["certificado"], encoding="ascii")
    arq_chave.touch(mode=0o600)
    arq_chave.write_bytes(chave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
    return str(cert), str(arq_chave)


def consultar_cuidador(rota: str, user_id: int, cab: dict, cert: tuple[str, str]) -> dict:
    """GET /cuidador/<user_id>/<rota> como o app do cuidador faz (token + certificado de cliente); resposta cifrada."""
    r = requests.get(f"{BASE}/cuidador/{user_id}/{rota}", headers=cab, verify=CA, cert=cert, timeout=15)
    r.raise_for_status()
    return decifrar(r)


def episodios_desde(historico: dict, instante: datetime) -> list[dict]:
    """Episodios do historico abertos DEPOIS de `instante` (para nao confundir com episodios antigos)."""
    return [e for e in historico["episodios"] if datetime.fromisoformat(e["abertoEm"]) >= instante]


# ---------------------------------------------------------------------------------------------------------------------
# ROTEIRO
# ---------------------------------------------------------------------------------------------------------------------
def main() -> None:
    saude = requests.get(f"{BASE}/health", verify=CA, timeout=10).json()
    if saude["analise"]["estado"] != "ok":
        sys.exit("A analise nao esta 'ok': suba-a antes (ela e quem decide a queda).")

    print("== Preparacao ==")
    tok_disp = login(CONTA_DISPOSITIVO, senha("FLOW_BRIDGE_SECRET", f"Senha de {CONTA_DISPOSITIVO}: "),
                     "wokwi", DEVICE_ID)
    tok_cuid = login(CONTA_CUIDADOR, senha("FLOW_CUIDADOR_SECRET", f"Senha de {CONTA_CUIDADOR}: "),
                     "app", DEVICE_ID_CUIDADOR)
    user_id = tok_disp["user_ids"][0]
    if user_id not in tok_cuid["user_ids"]:
        sys.exit(f"{CONTA_CUIDADOR} nao acompanha o user_id {user_id}: o cuidador nao veria o episodio.")
    cab_disp = {"Authorization": f"Bearer {tok_disp['access_token']}"}
    cab_cuid = {"Authorization": f"Bearer {tok_cuid['access_token']}"}
    seq = int(time.time())

    with tempfile.TemporaryDirectory(prefix="flow_v9_") as pasta:
        pasta = Path(pasta)
        cert_disp = pedir_certificado(tok_disp["access_token"], DEVICE_ID, pasta, "disp")
        cert_cuid = pedir_certificado(tok_cuid["access_token"], DEVICE_ID_CUIDADOR, pasta, "cuid")

        aberto = consultar_cuidador("estado", user_id, cab_cuid, cert_cuid)["episodioAberto"]
        if aberto:
            sys.exit(f"Ja existe um episodio aberto ({aberto['episodioId']}). Espere ele fechar e rode de novo.")

        def enviar(value: float, value_max: float) -> int:
            """Uma leitura do 'ESP32'. Repouso: value ~0,3. Impacto: valueMax 25 (acima do limiar de 20 m/s2 do wokwi)."""
            nonlocal seq
            seq += 1
            ev = {"schemaVersion": const_comum.SCHEMA_VERSION_WOKWI, "eventType": "imobilidade.leitura",
                  "deviceId": DEVICE_ID, "entityId": "idoso-simulado", "eventTimeMs": seq * 1000, "sequence": seq,
                  "value": value, "valueMax": value_max, "unit": "m/s2", "state": "NORMAL",
                  "user_id": user_id, "source": "wokwi"}
            return requests.post(f"{BASE}/dados", json=env(ev), headers=cab_disp, verify=CA, cert=cert_disp,
                                 timeout=15).status_code

        # PASSO 1: provocar a queda (e nao responder a ela)
        inicio = datetime.now(timezone.utc)
        print("\n== Provocando a queda: repouso, IMPACTO, repouso (e ninguem responde) ==")
        for _ in range(2):
            print(f"  repouso            -> HTTP {enviar(0.3, 0.4)}")
            time.sleep(1)
        print(f"  IMPACTO (pico 25)  -> HTTP {enviar(3.0, 25.0)}")
        time.sleep(1)

        # PASSO 2: o episodio deve abrir ~3 s depois (T_POS_IMPACTO_S); seguimos mandando repouso, como o firmware faria
        print("\n== Esperando o episodio abrir (o cuidador consulta /estado) ==")
        episodio = None
        t_fim = time.monotonic() + ESPERA_ABRIR_S
        while time.monotonic() < t_fim and episodio is None:
            enviar(0.3, 0.4)
            estado = consultar_cuidador("estado", user_id, cab_cuid, cert_cuid)
            ep = estado["episodioAberto"]
            if ep and datetime.fromisoformat(ep["abertoEm"]) >= inicio:
                episodio = ep
            else:
                print("  ainda sem episodio aberto...")
                time.sleep(1)
        if episodio is None:
            sys.exit("Nenhum episodio abriu. Confira: analise no ar? ultimo episodio ha menos de 10 s (cooldown)? "
                     "leituras chegando (HTTP 200)?")
        t_aberto = datetime.fromisoformat(episodio["abertoEm"])
        print(f"-> episodio {episodio['episodioId']} ABERTO ({episodio['tipo']}, estado={episodio['estado']}). "
              "Nao responda 'Estou bem'.")

        # PASSO 3: ninguem responde; o vigia da borda deve encerra-lo como EXPIRADO
        print(f"\n== Esperando o fim do prazo (esperado ~{PRAZO_ESPERADO_S} s) ==")
        encerrado = None
        t_fim = time.monotonic() + ESPERA_EXPIRAR_S
        while time.monotonic() < t_fim and encerrado is None:
            h = consultar_cuidador("historico", user_id, cab_cuid, cert_cuid)
            for e in h["episodios"]:
                if e["episodioId"] == episodio["episodioId"] and e["estado"] == "encerrado":
                    encerrado = e
            if encerrado is None:
                restante = (datetime.now(timezone.utc) - t_aberto).total_seconds()
                print(f"  aberto ha {restante:4.1f} s...")
                time.sleep(0.5)

    print("\n== Resultado do V9 ==")
    if encerrado is None:
        print("DIFERENTE: o episodio nao fechou no prazo. Confira o terminal da analise e da borda.")
        return
    duracao = (datetime.fromisoformat(encerrado["encerradoEm"]) - t_aberto).total_seconds()
    expirado = encerrado.get("resultado") == "EXPIRADO"
    print(f"Episodio {encerrado['episodioId']}: estado={encerrado['estado']} resultado={encerrado.get('resultado')} "
          f"duracao={duracao:.1f} s (esperado ~{PRAZO_ESPERADO_S} s)")
    print("Fechou como EXPIRADO e esta visivel ao cuidador no historico: " + ("OK" if expirado else "DIFERENTE"))
    print("Guarde esta saida (e, se quiser, os registros em 'episodios' e 'confirmacoes') como evidencia.")


if __name__ == "__main__":
    main()