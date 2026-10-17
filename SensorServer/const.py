"""
Constantes do SensorServer (fonte unica de configuracao).

Regras de uso:
  - Este modulo NAO importa nada do projeto (e um modulo folha).
  - Ao ser importado, carrega SensorServer/.env (ao lado deste arquivo) para o ambiente do processo; variavel
    de ambiente ja definida no terminal VENCE o arquivo. Ver .env.example e _carregar_dotenv().
  - Os demais modulos fazem `import const` e leem `const.X` NA HORA DO USO
    (nunca `from const import X`), para que um unico ajuste em `const.X` valha
    para todos os pontos de uso, inclusive em testes.
  - FLOW_JWT_SECRET NAO esta aqui: e segredo, nao constante (ver auth.py).
  - A chave AES e o tamanho do nonce ficam em crypto_utils.py (limitacao
    assumida, documentada la).
  - Politica e vocabulario (POLITICA, FONTE_DO_PAPEL, EVENTOS_ESPERADOS,
    EVENTOS_RETORNO, tipos de mensagem) ficam nos modulos que os usam.
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


DOTENV_ARQUIVO = Path(__file__).resolve().with_name(".env")
DOTENV_CARREGADO = _carregar_dotenv(DOTENV_ARQUIVO)

# ===========================================================================
# 1. SERVIDOR E AGREGACAO
# ===========================================================================

# Porta HTTP do servidor. O firewall do computador precisa liberar ENTRADA
# nesta porta (celular e bridge conectam no servidor).
PORT = 5000

# Pasta de dados: jsonl por usuario, alertas, auditoria e confirmacoes.
# Relativa ao diretorio de execucao. Definicao unica (antes duplicada em
# server.py, authz.py e regra_queda.py).
DATA_DIR = "dataUsers"

# Janela de agregacao app+wokwi, em segundos, antes de gravar uma linha em
# dataUsers/usuario_<id>.jsonl. Menor = mais linhas, mais granularidade.
JANELA_AGREGACAO_S = 1.0

# Periodo (s) da thread vigia que encerra episodios vencidos. Define o atraso
# maximo entre o prazo do servidor e o envio do encerramento EXPIRADO.
INTERVALO_VIGIA_S = 1.0

# Quantidade de registros recentes mantidos em memoria para o GET /dados
# (rota de depuracao, restrita a admin).
MAX_EM_MEMORIA = 200

# Fontes de telemetria aceitas. Definicao unica (antes em server.py e auth.py).
FONTES_VALIDAS = {"app", "wokwi"}

# Tamanho maximo (bytes) do corpo de qualquer requisicao. Acima disso o servidor responde 413 sem ler o
# resto. O maior corpo legitimo e um envelope cifrado de telemetria (poucos KB); 64 KiB deixa folga.
MAX_CONTENT_LENGTH = 64 * 1024

# Teto de entradas das tabelas em memoria cujas chaves vem do cliente. Ao estourar, as mais antigas saem.
# - auth: identificadores com falha de login registrada (_falhas) e identificadores bloqueados (_bloqueado_ate).
# - regras legadas (imobilidade e luz): estados por (user_id, device_id), um teto para cada tabela.
# Valores bem acima do uso real (poucos usuarios e dispositivos); servem so para o consumo de memoria
# nao crescer sem limite.
MAX_IDENTIFICADORES_LOGIN = 10_000
MAX_ESTADOS_LEGADOS = 1_000

# ===========================================================================
# 2. AUTENTICACAO
# ===========================================================================

# Validade do access token (s). Menor valor = mais renovacoes; ao vencer, o
# stream SSE e encerrado com o evento 'reautenticar'.
ACCESS_TTL_S = 3600

# Validade do refresh token (s), 7 dias. Depois disso o app pede nova
# autenticacao ao usuario e o bridge refaz o login com o segredo do ambiente.
REFRESH_TTL_S = 7 * 24 * 3600

# Falhas de login toleradas, por identificador, dentro de JANELA_FALHAS_S.
# Ao atingir este numero, o identificador fica bloqueado por BLOQUEIO_S.
MAX_FALHAS = 5

# Janela (s) de contagem de falhas de login: 15 minutos.
JANELA_FALHAS_S = 900

# Duracao (s) do bloqueio de login apos estourar MAX_FALHAS: 15 minutos.
BLOQUEIO_S = 900

# Arquivo de contas (coberto pelo *.json do .gitignore). Pode ser trocado pela
# variavel de ambiente FLOW_CONTAS_PATH.
CONTAS_PATH = os.environ.get("FLOW_CONTAS_PATH", "contas.json")

# Arquivo com o hash dos refresh tokens (sessoes persistem se o servidor
# reiniciar). Pode ser trocado por FLOW_SESSOES_PATH.
SESSOES_PATH = os.environ.get("FLOW_SESSOES_PATH", "sessoes.json")

# Papeis aceitos na criacao de contas. 'cuidador' e reservado (sem permissoes).
PAPEIS_VALIDOS = {"idoso", "dispositivo", "cuidador", "admin"}

# Custo do hash de senha (scrypt). Aumentar N encarece o login e tambem o
# ataque de forca bruta; mudar invalida a comparacao com hashes ja gravados
# se os parametros nao forem os mesmos de quando a conta foi criada.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1

# Tamanho maximo (caracteres) de device_id aceito no login (auth.device_id_valido).
# device_id vira chave do hub SSE; acima disso o login responde 400. Tambem
# limita o trecho de device_id usado em nomes de arquivo de alerta.
MAX_DEVICE_ID_LEN = 64

# ===========================================================================
# 3. AUTORIZACAO E AUDITORIA
# ===========================================================================

# Tamanho (bytes) a partir do qual auditoria.jsonl e rotacionado para
# auditoria.1.jsonl (sobrescrevendo o anterior). Evita encher o disco com
# negacoes repetidas: o espaco maximo fica em torno de 2x este valor.
AUDITORIA_MAX_BYTES = 5 * 1024 * 1024

# ===========================================================================
# 4. ESQUEMA E VALIDACAO
# ===========================================================================

# Versao MAIS RECENTE de cada contrato (docs/schemas.md). Uma constante por contrato, para subir a versao de um
# deles sem arrastar os demais. Unico lugar que escreve o numero. Payload com versao maior que a do seu
# contrato e descartado e logado; versao menor (>= SCHEMA_VERSION_MINIMA) e aceita.

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

# Teto plausivel de aceleracao (m/s2). Contrato comum de app e wokwi
# (MPU6050 em +-8 g). Leitura acima disso e invalida ou fora de faixa.
# Definicao unica (antes TETO_PLAUSIVEL e VALUE_MAX_PLAUSIVEL).
ACEL_MAX_PLAUSIVEL = 160.0

# Piso plausivel de aceleracao (m/s2). Magnitude nao e negativa.
ACEL_MIN_PLAUSIVEL = 0.0

# Aceleracao da gravidade (m/s2). Subtraida da magnitude do celular para obter
# a aceleracao DINAMICA (em repouso o celular mede ~9,81).
GRAVIDADE = 9.81

# ===========================================================================
# 5. DETECCAO DE QUEDA E CONFIRMACAO
# ===========================================================================

# FONTE UNICA do tempo (s) que o idoso tem para apertar "Estou bem". Vai na
# mensagem como timeoutMs; os dispositivos nao tem constante propria.
TIMEOUT_CONFIRMACAO_S = 5

# Folga (s) entre o prazo do dispositivo e o prazo do servidor. Prazo do
# episodio E expiresAt da mensagem = TIMEOUT_CONFIRMACAO_S + esta margem.
# Definicao unica (antes duplicada em mensagens.py e regra_queda.py).
MARGEM_SERVIDOR_S = 2

# Aceleracao dinamica (m/s2) a partir da qual uma leitura e IMPACTO, por
# fonte. Valores iniciais, a calibrar com dados reais.
LIMIAR_IMPACTO = {"wokwi": 20.0, "app": 15.0}

# Aceleracao dinamica (m/s2) abaixo da qual a leitura e REPOUSO, por fonte.
# Igual ou acima disso e movimento. Valores iniciais, a calibrar.
LIMIAR_REPOUSO = {"wokwi": 2.0, "app": 1.5}

# Imobilidade (s) exigida depois do impacto para abrir o episodio.
T_POS_IMPACTO_S = 3

# Depois de um impacto, se passar mais que isto (s) sem leitura aproveitavel,
# o impacto e descartado e a maquina volta a OBSERVANDO. A calibrar.
INTERVALO_MAX_ENTRE_LEITURAS_S = 3

# Idade maxima (s) da ultima leitura de uma fonte para participar da fusao de
# fontes. Deve ser menor ou igual a INTERVALO_MAX_ENTRE_LEITURAS_S.
JANELA_FUSAO_S = 2.0

# Intervalo minimo (s) entre episodios do mesmo usuario, contado a partir do
# encerramento do anterior.
COOLDOWN_S = 60

# No maximo um log de "leitura desatualizada" por (usuario, fonte, versao)
# neste intervalo (s), para nao inundar o console.
LOG_DESATUALIZADA_INTERVALO_S = 60

# Regressao (s) de timestamp_envio do app acima da qual se assume reajuste de
# relogio do celular e a referencia da deduplicacao e reiniciada
# (regra_queda.eh_duplicado_app).
TOLERANCIA_RELOGIO_S = 60

# Tempo (s) que o mapa messageId -> episodio guarda cada entrada DEPOIS de a
# mensagem expirar, para classificar respostas tardias. Menor valor = mapa
# menor, mas respostas muito atrasadas passam a ser gravadas como mensagem
# desconhecida. A poda roda na iteracao do vigia.
RETENCAO_MENSAGENS_S = 3600

# Maximo de dispositivos rastreados na deduplicacao da telemetria do app (device_id vem do payload do cliente).
# Passou disso, a referencia mais antiga e descartada (regra_queda.eh_duplicado_app).
MAX_REFERENCIAS_DEDUP = 1000

# Valores de 'motivo' na mensagem e no registro do episodio.
MOTIVO_A = "IMPACTO_SEGUIDO_DE_IMOBILIDADE"   # gatilho A (preferencial)
MOTIVO_B = "IMOBILIDADE_PROLONGADA"           # gatilho B

# ===========================================================================
# 6. MENSAGENS E PUSH (SSE)
# ===========================================================================

# Validade (s) da mensagem queda.confirmacao_encerrada. Deve ser MAIOR que
# TIMEOUT_CONFIRMACAO_S + MARGEM_SERVIDOR_S, para o encerramento sempre
# sobreviver ao pedido no replay apos reconexao.
TTL_ENCERRAMENTO_S = 10

# Intervalo (s) do comentario SSE de keepalive; permite aos dois lados
# detectar conexao morta.
KEEPALIVE_S = 15

# Antecedencia (s) do evento 'reautenticar' em relacao ao vencimento do token.
AVISO_REAUTENTICAR_S = 5

# Quantidade de mensagens guardadas por (usuario, canal) para replay.
TAMANHO_ANEL = 20

# Capacidade da fila de cada conexao SSE. Fila cheia: a conexao e fechada para
# forcar reconexao com Last-Event-ID. Menor valor = fecha mais cedo clientes
# lentos; deve ser pequeno (o anel de replay maior que a fila e truncado).
TAMANHO_FILA_CONEXAO = 16

# ===========================================================================
# 7. REGRA DE LUZ (MARCO 2)
# ===========================================================================

# Leitura com CCT (Kelvin) abaixo disto e descartada (fora de faixa).
CCT_MIN_VALIDO = 1000

# Leitura com CCT (Kelvin) acima disto e descartada (fora de faixa).
CCT_MAX_VALIDO = 12000

# CCT (Kelvin) acima do qual a luz e considerada "fria" (rica em azul).
CCT_LIMIAR_FRIO = 4500

# Janela noturna da regra de luz: das 18h as 06h.
HORA_INICIO_NOITE = 18
HORA_FIM_NOITE = 6

# Debounce: leituras consecutivas com a condicao presente para gerar alerta.
LEITURAS_CONSECUTIVAS_PARA_ALERTA = 3
