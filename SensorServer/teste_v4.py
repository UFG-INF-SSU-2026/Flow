"""
teste_v4.py - Verificacao manual V4 (identidade invalida) do projeto Flow.

Reproduz, pelo MESMO caminho do Wokwi (ESP32 -> bridge -> borda), tres situacoes em que a borda deve RECUSAR a chamada:

  sem-token        POST /dados sem o cabecalho Authorization        -> 401 token_ausente
  senha-errada     POST /auth/login com senha incorreta              -> 401 credenciais_invalidas
  sem-certificado  POST /dados com token VALIDO, mas sem certificado -> 401 certificado_ausente
                   de cliente (mTLS)
  todos            roda os tres, em ordem

Como rodar (dentro da pasta SensorServer/, com banco, borda e bridge no ar):

    python teste_v4.py sem-token
    python teste_v4.py senha-errada
    python teste_v4.py sem-certificado
    python teste_v4.py todos

Cada caso imprime o codigo HTTP recebido, o corpo da resposta e se bateu com o esperado (OK / DIFERENTE).
Como evidencia, guarde esta saida e a linha "[authz] negado ..." que aparece no terminal da BORDA.

ATENCAO (bloqueio): 5 senhas erradas em 15 min bloqueiam a conta (429) por 15 min, e isso tambem barra o login do Wokwi.
O caso "senha-errada" faz UMA tentativa. Para demonstrar o 429, use uma conta de teste (ver CONTA_DE_TESTE_429 abaixo).
Para destravar uma conta, reinicie a borda (o bloqueio fica so em memoria).
"""
import getpass
import json
import sys

import requests
import urllib3

# Estes dois imports vem do proprio projeto (SensorServer/borda/crypto_utils.py). E por isso que o script precisa ser
# executado dentro de SensorServer/: la o Python enxerga o pacote "borda".
from borda.crypto_utils import decrypt_payload, encrypt_payload

# ---------------------------------------------------------------------------------------------------------------------
# CONFIGURACAO (ajuste aqui se o seu ambiente for diferente)
# ---------------------------------------------------------------------------------------------------------------------

# Endereco do BRIDGE, a porta de entrada que o Wokwi usa. Chamar o bridge (e nao a borda direto) prova o caminho completo.
URL_BRIDGE = "https://127.0.0.1:5443"

# O certificado do bridge lista "host.wokwi.internal" (e nao 127.0.0.1). O Wokwi chama por esse nome; aqui mandamos o
# mesmo nome no cabecalho Host. (E equivalente ao --resolve do curl.)
CABECALHOS_BASE = {"Host": "host.wokwi.internal:5443", "Content-Type": "application/json"}

# Conta e dispositivo do firmware (os mesmos do segredos.h / criar-conta). O DEVICE_ID tem que ser o do sketch.ino.
CONTA_DO_FIRMWARE = "bridge1"
DEVICE_ID = "esp32-decisao-imobilidade-01"

# Conta descartavel para demonstrar o 429 sem travar a conta real. Crie antes com:
#   python -m borda.auth criar-conta --id teste1 --papel dispositivo --user-id 1 --fontes wokwi \
#          --device-id esp32-decisao-imobilidade-01
CONTA_DE_TESTE_429 = "teste1"

# Equivale ao "-k" do seu curl: nao confere o certificado do bridge. Aceitavel SO neste teste local; o firmware, o app
# e o bridge nunca desligam essa verificacao.
VERIFICAR_CERTIFICADO = False
if not VERIFICAR_CERTIFICADO:
    urllib3.disable_warnings()          # evita o aviso repetido de "InsecureRequestWarning"


# ---------------------------------------------------------------------------------------------------------------------
# FUNCOES AUXILIARES
# ---------------------------------------------------------------------------------------------------------------------

def cifrar(corpo: dict) -> dict:
    """Monta o ENVELOPE que a borda espera nas rotas de login: o JSON e cifrado com AES-256-GCM (chave compartilhada) e
    vai como {"nonce": ..., "ciphertext": ...}. Por isso o curl puro nao serve para o login: ele nao cifra."""
    nonce, texto_cifrado = encrypt_payload(json.dumps(corpo))
    return {"nonce": nonce, "ciphertext": texto_cifrado}


def decifrar(resposta: requests.Response) -> dict:
    """Operacao inversa: as respostas de sucesso da borda tambem vem cifradas no mesmo formato."""
    envelope = resposta.json()
    return json.loads(decrypt_payload(envelope["nonce"], envelope["ciphertext"]))


def fazer_login(identificador: str, segredo: str) -> requests.Response:
    """POST /auth/login exatamente como o firmware faz (sessao.h): mesmos campos, fonte "wokwi" e o device_id do sketch.
    Nao leva certificado de cliente: o login e a unica chamada que dispensa o certificado."""
    corpo = {
        "schemaVersion": 3,             # SESSAO_SCHEMA_AUTH no sessao.h
        "identificador": identificador,
        "segredo": segredo,
        "fonte": "wokwi",
        "device_id": DEVICE_ID,
    }
    return requests.post(f"{URL_BRIDGE}/auth/login", json=cifrar(corpo),
                         headers=CABECALHOS_BASE, verify=VERIFICAR_CERTIFICADO, timeout=15)


def mostrar(titulo: str, resposta: requests.Response, codigo_esperado: int, texto_esperado: str) -> bool:
    """Imprime o resultado e compara com o esperado. Devolve True se o codigo HTTP E o motivo batem."""
    bateu = resposta.status_code == codigo_esperado and texto_esperado in resposta.text
    print(f"\n=== {titulo} ===")
    print(f"HTTP {resposta.status_code}  corpo: {resposta.text.strip()}")
    print(f"esperado: {codigo_esperado} com '{texto_esperado}'  ->  {'OK' if bateu else 'DIFERENTE (confira os logs)'}")
    return bateu


# ---------------------------------------------------------------------------------------------------------------------
# OS TRES CASOS
# ---------------------------------------------------------------------------------------------------------------------

def caso_sem_token() -> bool:
    """CASO 1 - Nao ha token entre o Wokwi e o servidor.

    Manda POST /dados SEM o cabecalho "Authorization: Bearer <token>". A borda verifica o token ANTES de qualquer outra
    coisa (inclusive antes de decifrar o corpo), entao o corpo pode ser qualquer coisa: aqui vai um "{}" simples.
    Esperado: 401 token_ausente (authz.py)."""
    resposta = requests.post(f"{URL_BRIDGE}/dados", data=b"{}", headers=CABECALHOS_BASE,
                             verify=VERIFICAR_CERTIFICADO, timeout=15)
    return mostrar("Caso 1: sem token", resposta, 401, "token_ausente")


def caso_senha_errada() -> bool:
    """CASO 2 - Senha errada no login.

    Tenta entrar com uma senha que com certeza nao e a da conta. Esperado: 401 credenciais_invalidas.
    Cuidado: conta como UMA falha (o limite e 5 em 15 min, depois vem o 429)."""
    resposta = fazer_login(CONTA_DO_FIRMWARE, "senha-errada-de-proposito")
    return mostrar(f"Caso 2: senha errada ({CONTA_DO_FIRMWARE})", resposta, 401, "credenciais_invalidas")


def caso_sem_certificado() -> bool:
    """CASO 3 - Token VALIDO, mas sem certificado de cliente (mTLS).

    Passo 1: faz login com a senha real e obtem um access_token valido (isso o firmware tambem faz antes de ter certificado).
    Passo 2: chama POST /dados com esse token, mas SEM apresentar certificado de cliente (nao passamos o parametro
    cert=... do requests). A borda ve que o token e bom, e ai exige o certificado: 401 certificado_ausente.
    No firmware real, e essa resposta que faz o ESP32 pedir a emissao do proprio certificado."""
    senha = getpass.getpass(f"Senha real de {CONTA_DO_FIRMWARE} (nao e exibida): ")
    login = fazer_login(CONTA_DO_FIRMWARE, senha)
    if login.status_code != 200:
        print(f"\nLogin falhou (HTTP {login.status_code}: {login.text.strip()}). Confira a senha, ou se a conta esta "
              f"bloqueada (429): reinicie a borda.")
        return False
    access_token = decifrar(login)["access_token"]

    cabecalhos = {**CABECALHOS_BASE, "Authorization": f"Bearer {access_token}"}   # agora o token EXISTE
    resposta = requests.post(f"{URL_BRIDGE}/dados", data=b"{}", headers=cabecalhos,
                             verify=VERIFICAR_CERTIFICADO, timeout=15)           # ...mas nao ha cert=(crt, key)
    return mostrar("Caso 3: token valido, sem certificado de cliente", resposta, 401, "certificado_ausente")


def demonstrar_429() -> None:
    """EXTRA - Bloqueio depois de 5 senhas erradas (429).

    Usa a CONTA_DE_TESTE_429, nao a do firmware, para nao travar o login do Wokwi. Na sexta tentativa a borda responde
    429 bloqueado (por 15 minutos; reiniciar a borda desfaz)."""
    print(f"\n=== Extra: bloqueio por tentativas ({CONTA_DE_TESTE_429}) ===")
    for tentativa in range(1, 7):
        r = fazer_login(CONTA_DE_TESTE_429, "errada")
        print(f"tentativa {tentativa}: HTTP {r.status_code}  {r.text.strip()}")


# ---------------------------------------------------------------------------------------------------------------------
# PONTO DE ENTRADA
# ---------------------------------------------------------------------------------------------------------------------

CASOS = {"sem-token": caso_sem_token, "senha-errada": caso_senha_errada, "sem-certificado": caso_sem_certificado}


def main() -> None:
    escolha = sys.argv[1] if len(sys.argv) > 1 else ""
    if escolha == "429":
        demonstrar_429()
    elif escolha == "todos":
        resultados = {nome: funcao() for nome, funcao in CASOS.items()}
        print("\n=== Resumo ===")
        for nome, ok in resultados.items():
            print(f"{nome:16} {'OK' if ok else 'DIFERENTE'}")
    elif escolha in CASOS:
        CASOS[escolha]()
    else:
        sys.exit("Uso: python teste_v4.py [sem-token | senha-errada | sem-certificado | todos | 429]")


if __name__ == "__main__":
    main()