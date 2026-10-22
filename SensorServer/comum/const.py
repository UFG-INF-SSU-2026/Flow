"""
Constantes COMUNS do SensorServer: so o que a borda E a analise precisam enxergar igual, mais o carregador do .env.

Divisao da configuracao (roadmap do split, principio 8):
  comum/const.py     (este)  pasta de dados, fontes, versoes dos contratos, prazos do push, pausa do banco
  borda/const.py             rede, autenticacao, auditoria, SSE, descarga de leituras
  analise/const.py           limiares e prazos das regras (queda, imobilidade, luz) e o periodo do vigia

Regras de uso:
  - Este modulo NAO importa nada do projeto (e um modulo folha). `borda/const.py` e `analise/const.py` so importam
    este, e so para que o .env ja esteja carregado quando elas leem o ambiente.
  - Ao ser importado, carrega SensorServer/.env para o ambiente do processo; variavel de ambiente ja definida no
    terminal VENCE o arquivo. Ver .env.example e _carregar_dotenv(). Enquanto borda e analise forem o mesmo processo
    ha um .env so; na separacao (S5) cada servico passa a ler o seu.
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

from dotenv import load_dotenv


def _carregar_dotenv(caminho: Path) -> bool:
    """Carrega `caminho` (um .env) para os.environ SEM sobrescrever o que ja esta definido: variavel de ambiente do
    terminal vence o arquivo. Retorna True se o arquivo foi lido; arquivo ausente nao e erro (o ambiente pode bastar).

    - utf-8-sig: um .env salvo com BOM (Bloco de Notas do Windows) nao estraga o nome da primeira chave;
    - interpolate=False: `$` e `${...}` num segredo ou senha ficam literais;
    - FLOW_SEM_DOTENV=1 desliga a leitura (usado pela suite de testes, para nao ler o .env real).
    """
    if os.environ.get("FLOW_SEM_DOTENV") == "1" or not caminho.is_file():
        return False
    return load_dotenv(caminho, override=False, interpolate=False, encoding="utf-8-sig")


# O .env fica em SensorServer/ (a pasta acima de comum/).
DOTENV_ARQUIVO = Path(__file__).resolve().parent.parent / ".env"
DOTENV_CARREGADO = _carregar_dotenv(DOTENV_ARQUIVO)

# ===========================================================================
# 1. PASTA DE DADOS E FONTES
# ===========================================================================

# Pasta de dados: jsonl por usuario, alertas, auditoria e confirmacoes.
# Relativa ao diretorio de execucao. Definicao unica (a borda grava usuario_*.jsonl e auditoria; a analise grava
# alertas e confirmacoes).
DATA_DIR = "dataUsers"

# Dupla escrita (fase de transicao): enquanto 1, o servidor continua gravando os arquivos de hoje (usuario_*.jsonl,
# alerta_*.json, confirmacoes_*.jsonl, auditoria.jsonl) ALEM do banco. Desligue com FLOW_GRAVAR_ARQUIVOS=0 quando o
# banco for a unica fonte.
GRAVAR_ARQUIVOS = os.environ.get("FLOW_GRAVAR_ARQUIVOS", "1") != "0"

# Fontes de telemetria aceitas. Definicao unica.
FONTES_VALIDAS = {"app", "wokwi"}

# Tamanho maximo (caracteres) de device_id aceito no login (borda/auth.device_id_valido).
# device_id vira chave do hub SSE; acima disso o login responde 400. Tambem
# limita o trecho de device_id usado em nomes de arquivo de alerta.
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
# 4. BANCO (PostgreSQL + TimescaleDB). Conexao e senhas: flow_db/config.py e .env.
# ===========================================================================

# Apos uma falha do banco, as escritas (fora a descarga em lote) sao puladas por este tempo (s), para a requisicao
# nao esperar o tempo limite do pool a cada chamada durante uma queda. Vale para os dois lados (flow_db/repositorios.py).
DB_PAUSA_APOS_FALHA_S = 5.0

# Sinalizador do CUTOVER das acoes (roadmap do split, S4). 0 (padrao): o caminho direto decide, grava e entrega (como
# sempre). 1: a ANALISE (laco por cursor) decide e grava `comandos_push`, `alertas`, `confirmacoes`, `episodios` e
# `estado_dispositivo` na mesma transacao do cursor; a BORDA le `comandos_push` por cursor, entrega por SSE e grava
# `entregas_push`. Exige o laco ligado (FLOW_LACO_ANALISE=1), senao ninguem decidiria: sem ele o sinalizador e ignorado
# (com aviso). Voltar ao caminho direto = FLOW_ACOES_PELO_BANCO=0 e reiniciar. Fica ate o fim da S5.
ACOES_PELO_BANCO = os.environ.get("FLOW_ACOES_PELO_BANCO", "0") == "1"
