"""
Constantes da BORDA (gateway): rede, autenticacao, autorizacao, SSE e descarga de leituras.

Este modulo so importa `comum.const`, e apenas para que o .env ja esteja carregado quando ele le o ambiente. Quem usa
importa o modulo (`from borda import const as const_borda`) e le `const_borda.X` na hora do uso. As constantes comuns
estao em comum/const.py; as das regras, em analise/const.py. Ver comum/const.py para as regras de uso.
"""
import os

from comum import const as _comum  # noqa: F401  (efeito colateral: o .env e carregado ANTES de ler o ambiente abaixo)

# ===========================================================================
# 1. SERVIDOR E AGREGACAO
# ===========================================================================

# Porta HTTP do servidor. O firewall do computador precisa liberar ENTRADA
# nesta porta (celular e bridge conectam no servidor).
PORT = 5000

# Janela de agregacao app+wokwi, em segundos, antes de gravar uma linha em
# dataUsers/usuario_<id>.jsonl. Menor = mais linhas, mais granularidade.
JANELA_AGREGACAO_S = 1.0

# Quantidade de registros recentes mantidos em memoria para o GET /dados
# (rota de depuracao, restrita a admin).
MAX_EM_MEMORIA = 200

# Tamanho maximo (bytes) do corpo de qualquer requisicao. Acima disso o servidor responde 413 sem ler o
# resto. O maior corpo legitimo e um envelope cifrado de telemetria (poucos KB); 64 KiB deixa folga.
MAX_CONTENT_LENGTH = 64 * 1024

# Teto de identificadores com falha de login registrada (_falhas) e de identificadores bloqueados (_bloqueado_ate).
# Os identificadores vem do cliente: ao estourar, os mais antigos saem. Valor bem acima do uso real; serve so para o
# consumo de memoria nao crescer sem limite.
MAX_IDENTIFICADORES_LOGIN = 10_000

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

# ===========================================================================
# 3. AUTORIZACAO E AUDITORIA
# ===========================================================================

# Tamanho (bytes) a partir do qual auditoria.jsonl e rotacionado para
# auditoria.1.jsonl (sobrescrevendo o anterior). Evita encher o disco com
# negacoes repetidas: o espaco maximo fica em torno de 2x este valor.
AUDITORIA_MAX_BYTES = 5 * 1024 * 1024

# ===========================================================================
# 4. PUSH (SSE)
# ===========================================================================

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
# 5. BANCO: DESCARGA DE LEITURAS (a borda e a unica escritora de `leituras`)
# ===========================================================================

# Dupla escrita (fase de transicao): FLOW_GRAVAR_TELEMETRIA_LEGADO=0 para de gravar a tabela `telemetria`
# (1 linha/usuario/janela). Os arquivos de hoje sao controlados por comum/const.py (GRAVAR_ARQUIVOS).
GRAVAR_TELEMETRIA_LEGADO = os.environ.get("FLOW_GRAVAR_TELEMETRIA_LEGADO", "1") != "0"

# Descarga das leituras: a thread de fundo grava em LOTES a cada INTERVALO_DESCARGA_S, ate TAMANHO_LOTE_LEITURAS por vez.
INTERVALO_DESCARGA_S = 0.25
TAMANHO_LOTE_LEITURAS = 500

# Fila de leituras em memoria (limitada). Com o banco fora do ar a borda segue aceitando dados; ao estourar o teto,
# as leituras MAIS ANTIGAS saem (e o descarte e contado e logado). 20 mil leituras = bem mais que minutos de uso real.
MAX_FILA_LEITURAS = 20_000

# ===========================================================================
# 6. ENTREGA DE PUSH PELO BANCO (S4 do split; so com comum.const.ACOES_PELO_BANCO)
# ===========================================================================

# Espera (s) entre dois ciclos de leitura de `comandos_push`: a terceira espera do orcamento de latencia (roadmap, secao 9).
# Se o lote veio cheio a borda nao espera.
INTERVALO_LEITURA_COMANDOS_S = 0.25

# Comandos lidos por ciclo.
LOTE_COMANDOS = 100

# Comando criado ha mais que isto (s) nao e mais lido: ja expirou (o prazo do pedido e o TTL do encerramento sao bem
# menores). Evita reentregar historico quando a borda volta de uma parada ou o cursor ainda esta em zero.
JANELA_COMANDOS_S = 60

# Falha repetida da entrega (banco fora do ar): no maximo uma linha de log neste intervalo (s).
INTERVALO_LOG_FALHA_ENTREGA_S = 10
