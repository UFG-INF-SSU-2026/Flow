"""
Constantes COMUNS do SensorServer: so o que a borda E a analise precisam enxergar igual, mais o carregador do .env.

Divisao da configuracao:
  comum/const.py     (este)  fontes, versoes dos contratos, prazos do push, endereco da analise
  borda/const.py             rede, autenticacao, SSE, descarga de leituras, chamada a analise
  analise/const.py           limiares e prazos das regras (queda, imobilidade, luz)

Regras de uso:
  - Este modulo NAO importa nada do projeto (e um modulo folha). `borda/const.py` e `analise/const.py` so importam
    este, e so para que o .env ja esteja carregado quando elas leem o ambiente.
  - Ao ser importado, carrega SensorServer/.env para o ambiente do processo; variavel de ambiente ja definida no
    terminal VENCE o arquivo. Ver .env.example e _carregar_dotenv(). Ha UM so .env. A analise
    (FLOW_SERVICO=analise) copia dele SO as variaveis FLOW_ANALISE_* e FLOW_TLS_* (caminhos da CA, do certificado e da chave): o segredo
    JWT e a senha do banco nunca entram no ambiente dela.
  - Os demais modulos importam o modulo (`from comum import const as const_comum`) e leem `const_comum.X` NA HORA DO
    USO (nunca `from comum.const import X`), para que um unico ajuste valha para todos os pontos de uso, inclusive
    em testes. Cada constante tem UM dono: ajuste-a no modulo do dono.
  - FLOW_JWT_SECRET NAO esta aqui: e segredo, nao constante (ver borda/auth.py).
  - A chave AES e o tamanho do nonce ficam em borda/crypto_utils.py (limitacao assumida, documentada la).
  - Politica (POLITICA, FONTE_DO_PAPEL) fica nos modulos que a usam; o vocabulario dos eventos (eventType/state) e os
    campos obrigatorios vivem em schemas/ (fonte unica, ver schemas/README.md).
"""
import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv


def _carregar_dotenv(caminho: Path, prefixo: str | tuple[str, ...] | None = None) -> bool:
    """Carrega `caminho` (um .env) para os.environ SEM sobrescrever o que ja esta definido: variavel de ambiente do
    terminal vence o arquivo. Retorna True se o arquivo foi lido; arquivo ausente nao e erro (o ambiente pode bastar).

    - `prefixo`: carrega SO as variaveis cujo nome comeca com ele (ou com um dos itens, se for uma tupla); a analise usa
      "FLOW_ANALISE_" e "FLOW_TLS_";
    - utf-8-sig: um .env salvo com BOM (Bloco de Notas do Windows) nao estraga o nome da primeira chave;
    - interpolate=False: `$` e `${...}` num segredo ou senha ficam literais;
    - FLOW_SEM_DOTENV=1 desliga a leitura (usado pela suite de testes, para nao ler o .env real).
    """
    if os.environ.get("FLOW_SEM_DOTENV") == "1" or not caminho.is_file():
        return False
    if prefixo is None:
        return load_dotenv(caminho, override=False, interpolate=False, encoding="utf-8-sig")
    for nome, valor in dotenv_values(caminho, interpolate=False, encoding="utf-8-sig").items():
        if nome.startswith(prefixo) and valor is not None:
            os.environ.setdefault(nome, valor)
    return True


# Servico que este processo e. Os pontos de entrada `python -m borda` e `python -m analise` definem FLOW_SERVICO
# ANTES de importar qualquer modulo do projeto. Vazio = ferramentas e modo de desenvolvimento (`python -m borda.server`,
# `python -m flow_db`). A analise le do .env so o que comeca com FLOW_ANALISE_.
SERVICO = os.environ.get("FLOW_SERVICO", "")
if SERVICO not in ("", "borda", "analise"):
    raise RuntimeError(f"FLOW_SERVICO invalido: {SERVICO!r} (use 'borda' ou 'analise', ou deixe vazio)")

# O arquivo fica em SensorServer/ (a pasta acima de comum/).
DOTENV_ARQUIVO = Path(__file__).resolve().parent.parent / ".env"
DOTENV_CARREGADO = _carregar_dotenv(DOTENV_ARQUIVO, ("FLOW_ANALISE_", "FLOW_TLS_") if SERVICO == "analise" else None)

# ===========================================================================
# 1. FONTES
# ===========================================================================

# Fontes de telemetria aceitas. Definicao unica.
FONTES_VALIDAS = {"app", "wokwi"}

# Tamanho maximo (caracteres) de device_id aceito no login (borda/auth.device_id_valido).
# device_id vira chave do hub SSE; acima disso o login responde 400.
MAX_DEVICE_ID_LEN = 64

# ===========================================================================
# 2. VERSOES DOS CONTRATOS (schemas/, docs/schemas.md)
# ===========================================================================

# Versao MAIS RECENTE de cada contrato. Uma constante por contrato, para subir a versao de um deles sem arrastar os
# demais. Unico lugar que escreve o numero. Payload com versao maior que a do seu contrato e descartado e logado;
# versao menor (>= SCHEMA_VERSION_MINIMA) e aceita.

# App (SensorApp): /dados source=app, leitura_ambiente e eventos de retorno vindos do app.
SCHEMA_VERSION_APP = 3

# Wokwi (firmware e WokwiBridge): /dados source=wokwi e eventos de retorno vindos do firmware/bridge.
SCHEMA_VERSION_WOKWI = 3

# Mensagem: push que o servidor envia (confirmacao.solicitada / confirmacao.encerrada). App e bridge
# precisam suportar esta versao; ela NAO acompanha as demais.
SCHEMA_VERSION_MENSAGEM = 3

# Auth: /auth/login e /auth/refresh. Compartilhado por app e bridge e o payload nao traz `source`,
# por isso nao da para seguir APP ou WOKWI.
SCHEMA_VERSION_AUTH = 3

# Menor versao que o servidor ainda aceita (vale para app, wokwi e auth). Abaixo disso o payload e descartado.
# Versao menor que a do contrato, mas >= esta, e aceita e usada.
SCHEMA_VERSION_MINIMA = 1

# Versao assumida quando o payload nao traz schemaVersion (produtor antigo).
SCHEMA_VERSION_PADRAO = 1

# ===========================================================================
# 3. PRAZOS DO PEDIDO DE CONFIRMACAO (contrato da mensagem de push)
# ===========================================================================
# A analise decide e monta a mensagem; a borda a entrega e recalcula o tempo restante no replay. Os tres valores
# definem a linha do tempo da mensagem, por isso moram juntos.

# FONTE UNICA do tempo (s) que o idoso tem para apertar "Estou bem". Vai na
# mensagem como timeoutMs; os dispositivos nao tem constante propria.
TIMEOUT_CONFIRMACAO_S = 5

# Folga (s) entre o prazo do dispositivo e o prazo do servidor. Prazo do
# episodio E expiresAt da mensagem = TIMEOUT_CONFIRMACAO_S + esta margem.
# Definicao unica (antes duplicada em mensagens.py e regra_queda.py).
MARGEM_SERVIDOR_S = 2

# Validade (s) da mensagem queda.confirmacao_encerrada. Deve ser MAIOR que
# TIMEOUT_CONFIRMACAO_S + MARGEM_SERVIDOR_S, para o encerramento sempre
# sobreviver ao pedido no replay apos reconexao.
TTL_ENCERRAMENTO_S = 10

# ===========================================================================
# 4. ANALISE (Servidor 2): API HTTPS local, so na maquina
# ===========================================================================

# A analise escuta so em ANALISE_HOST (loopback), em HTTPS; a borda a chama em FLOW_ANALISE_URL (padrao: este endereco e porta).
ANALISE_HOST = "127.0.0.1"
ANALISE_PORT = int(os.environ.get("FLOW_ANALISE_PORT", "5100"))
ANALISE_CABECALHO_TOKEN = "X-Flow-Analise-Token"


def token_analise() -> str:
    """Token compartilhado entre a borda e a analise (FLOW_ANALISE_TOKEN, minimo 16 caracteres). Lido na hora do uso."""
    token = os.environ.get("FLOW_ANALISE_TOKEN", "")
    if len(token) < 16:
        raise RuntimeError("FLOW_ANALISE_TOKEN ausente ou com menos de 16 caracteres. Defina-o no .env (modelo: "
                           ".env.example); a borda e a analise usam o mesmo valor.")
    return token


def url_analise() -> str:
    return os.environ.get("FLOW_ANALISE_URL") or f"https://{ANALISE_HOST}:{ANALISE_PORT}"


# ===========================================================================
# 5. TLS (CA propria do projeto, em todas as pontas)
# ===========================================================================

# A CA do projeto (scripts/gerar_certificado.py) assina um certificado por servico: a borda, a analise e o banco apresentam
# o seu, e a CA (certs/ca.crt) e a UNICA confianca dos clientes (borda -> analise, borda -> banco, bridge, app). Os caminhos
# podem ser trocados por FLOW_TLS_CA (confianca) e FLOW_TLS_CERT/FLOW_TLS_KEY (o par de quem serve); caminho relativo vale a
# partir de SensorServer/ (nao de onde o processo foi iniciado). `{servico}` e a borda ou a analise, conforme FLOW_SERVICO
# (vazio, o modo de desenvolvimento, e a borda). A chave da CA (ca.key) so o script de geracao le.
TLS_CA_PADRAO = "certs/ca.crt"
TLS_CERT_PADRAO = "certs/{servico}.crt"
TLS_CHAVE_PADRAO = "certs/{servico}.key"
