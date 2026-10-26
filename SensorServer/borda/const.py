"""
Constantes da BORDA (Servidor 1): rede, autenticacao, autorizacao, SSE, descarga de leituras e chamada a analise.

Este modulo so importa `comum.const`, e apenas para que o .env ja esteja carregado quando ele le o ambiente. Quem usa
importa o modulo (`from borda import const as const_borda`) e le `const_borda.X` na hora do uso. As constantes comuns
estao em comum/const.py; as das regras, em analise/const.py. Ver comum/const.py para as regras de uso.
"""
import os

from comum import const as _comum  # noqa: F401  (efeito colateral: o .env e carregado ANTES de ler o ambiente abaixo)


# ===========================================================================
# 1. SERVIDOR E AGREGACAO
# ===========================================================================

# Onde a borda escuta (so HTTPS). Padrao: 127.0.0.1, ou seja, so este computador. Para o app e o bridge em outra maquina,
# defina FLOW_BORDA_HOST com o IP do computador na rede (o mesmo que esta no certificado da borda; ver .env.example) e
# libere a ENTRADA na porta no firewall. FLOW_BORDA_PORT troca a porta (padrao 5000).
HOST = os.environ.get("FLOW_BORDA_HOST") or "127.0.0.1"
PORT = int(os.environ.get("FLOW_BORDA_PORT") or 5000)

# Quantas leituras o GET /dados (rota de depuracao, so admin; le a tabela `leituras`) devolve por padrao e no maximo.
LIMITE_GET_DADOS = 200

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

# Papeis aceitos na criacao de contas. 'cuidador' e reservado (sem permissoes).
PAPEIS_VALIDOS = {"idoso", "dispositivo", "cuidador", "admin"}

# Custo do hash de senha (scrypt). Aumentar N encarece o login e tambem o
# ataque de forca bruta; mudar invalida a comparacao com hashes ja gravados
# se os parametros nao forem os mesmos de quando a conta foi criada.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1

# ===========================================================================
# 3. PUSH (SSE)
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
# 4. LEITURAS: DESCARGA NO BANCO E ENVIO A ANALISE (a borda e a unica escritora)
# ===========================================================================

# A thread de fundo faz um passo a cada INTERVALO_DESCARGA_S: grava as leituras em LOTE no banco, manda o lote a analise e
# grava as decisoes. TAMANHO_LOTE_LEITURAS e o maximo de itens por lote.
INTERVALO_DESCARGA_S = 0.25
TAMANHO_LOTE_LEITURAS = 500

# Fila de leituras ainda nao gravadas (limitada). Com o banco fora do ar a borda segue aceitando dados; ao estourar o teto,
# as leituras MAIS ANTIGAS saem (o descarte e contado e logado). 20 mil leituras = bem mais que minutos de uso real.
MAX_FILA_LEITURAS = 20_000

# Fila de itens ja gravados que esperam a analise (limitada, pelo mesmo motivo). Com a analise fora do ar a borda segue
# aceitando e gravando; ao voltar, a analise recebe a fila (item velho so reconstroi estado, ver IDADE_MAX_ACAO_S).
MAX_FILA_ANALISE = 5_000

# ===========================================================================
# 5. CHAMADA A ANALISE (Servidor 2)
# ===========================================================================

# Prazo (s) de cada chamada a analise; /restaurar leva mais dados e tem prazo proprio. Estourou: a analise conta como
# fora do ar e o lote fica na fila.
ANALISE_TIMEOUT_S = 1.0
ANALISE_TIMEOUT_RESTAURAR_S = 5.0

# O vigia (expiracao de episodios) roda a cada INTERVALO_VIGIA_S e usa o relogio real MENOS ATRASO_VIGIA_S: cobre o dado
# ainda em transito (fila de descarga + analise), para nao expirar um episodio cuja resposta ja chegou mas ainda nao foi
# analisada. O atraso maximo entre o prazo do servidor e o encerramento EXPIRADO e a soma dos dois.
INTERVALO_VIGIA_S = 0.25
ATRASO_VIGIA_S = 0.5

# Restauracao do estado da analise (ao subir e quando o `boot_id` da analise muda): a borda manda as leituras dos ultimos
# JANELA_RESTAURACAO_S segundos (ate LIMITE_RESTAURACAO, as mais recentes), os episodios abertos e os recentes (atualizados
# na ultima RETENCAO_EPISODIOS_S) e as mensagens deles. Tem de cobrir a maior janela das regras (fusao, pos-impacto,
# imobilidade, debounce da luz, deduplicacao).
JANELA_RESTAURACAO_S = 30
LIMITE_RESTAURACAO = 5_000
RETENCAO_EPISODIOS_S = 3600

# Falha repetida (banco ou analise fora do ar): no maximo uma linha de log neste intervalo (s).
INTERVALO_LOG_FALHA_S = 10

# ===========================================================================
# 6. DESLIGAMENTO ORDENADO
# ===========================================================================

# Desligamento ordenado: tempo maximo (s) para a descarga final das leituras e para as threads terminarem.
PRAZO_ENCERRAMENTO_S = 10
