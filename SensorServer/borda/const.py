"""
Constantes da BORDA (gateway): rede, autenticacao, autorizacao, SSE e descarga de leituras.

Este modulo so importa `comum.const`, e apenas para que o .env ja esteja carregado quando ele le o ambiente. Quem usa
importa o modulo (`from borda import const as const_borda`) e le `const_borda.X` na hora do uso. As constantes comuns
estao em comum/const.py; as das regras, em analise/const.py. Ver comum/const.py para as regras de uso.
"""
import os

from comum import const as _comum  # noqa: F401  (efeito colateral: o .env e carregado ANTES de ler o ambiente abaixo)


def _numero_do_ambiente(nome: str, padrao: float):
    """Numero positivo vindo da variavel `nome` (ou `padrao`). Valor invalido derruba a subida com mensagem clara: um
    limiar de saude errado em silencio faria o /health mentir."""
    bruto = os.environ.get(nome)
    if bruto in (None, ""):
        return padrao
    try:
        valor = float(bruto)
    except ValueError:
        raise RuntimeError(f"{nome} invalida: {bruto!r} (esperado um numero maior que zero)") from None
    if not valor > 0:
        raise RuntimeError(f"{nome} invalida: {bruto!r} (esperado um numero maior que zero)")
    return int(valor) if isinstance(padrao, int) and valor == int(valor) else valor


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
# 6. ENTREGA DE PUSH PELO BANCO
# ===========================================================================

# Espera (s) entre dois ciclos de leitura de `comandos_push`: a terceira espera do orcamento de latencia (ver analise/const.py, ORCAMENTO_LATENCIA_S).
# Se o lote veio cheio a borda nao espera.
INTERVALO_LEITURA_COMANDOS_S = 0.25

# Comandos lidos por ciclo.
LOTE_COMANDOS = 100

# Comando criado ha mais que isto (s) nao e mais lido: ja expirou (o prazo do pedido e o TTL do encerramento sao bem
# menores). Evita reentregar historico quando a borda volta de uma parada ou o cursor ainda esta em zero.
JANELA_COMANDOS_S = 60

# Falha repetida da entrega (banco fora do ar): no maximo uma linha de log neste intervalo (s).
INTERVALO_LOG_FALHA_ENTREGA_S = 10

# ===========================================================================
# 7. DESLIGAMENTO ORDENADO
# ===========================================================================

# Desligamento ordenado: tempo maximo (s) para a descarga final das leituras e para as threads terminarem.
PRAZO_ENCERRAMENTO_S = 10

# ===========================================================================
# 8. OBSERVABILIDADE: /health E ALARMES
# ===========================================================================

# O /health acusa o pipeline "degradado" quando a analise passa deste tempo (s) sem renovar o batimento em
# `cursor_analise`. A analise renova a cada ciclo (INTERVALO_LEITURA_S, 0,25 s), mesmo sem dado novo: so a analise parada,
# presa ou sem banco passa disto. Fica abaixo do prazo do pedido de confirmacao (TIMEOUT + MARGEM): a analise parada
# e acusada antes de um pedido perdido expirar. FLOW_LIMIAR_BATIMENTO_S ajusta.
LIMIAR_BATIMENTO_ANALISE_S = _numero_do_ambiente("FLOW_LIMIAR_BATIMENTO_S", 5.0)

# Atraso (em leituras, id da ultima leitura gravada menos o do cursor da analise) a partir do qual o /health acusa
# "degradado". O valor inicial equivale a cerca de 30 s de duas fontes a 1 leitura/s: acima disso a analise ja esta
# descartando acoes por idade (analise/const.py, IDADE_MAX_ACAO_S). ENTRADA DA RETENCAO DO BANCO (parte 2, B2.4): o
# limiar de atraso e o que a retencao das tabelas precisa superar com folga. A calibrar com medicao.
# FLOW_LIMIAR_ATRASO_LEITURAS ajusta.
LIMIAR_ATRASO_LEITURAS = _numero_do_ambiente("FLOW_LIMIAR_ATRASO_LEITURAS", 60)

# A thread de monitoramento da borda consulta a saude a cada INTERVALO_MONITOR_SAUDE_S (s) e escreve os alarmes no log.
INTERVALO_MONITOR_SAUDE_S = 2.0

# O /health reaproveita a ultima consulta por CACHE_SAUDE_S (s): a rota e publica e nao pode virar consulta ao banco por
# requisicao. 0 desliga o cache (os testes).
CACHE_SAUDE_S = 1.0

# Enquanto o pipeline segue degradado, o alarme se repete no log a cada INTERVALO_REPETIR_ALARME_S (s) (a entrada, a
# mudanca de motivo e a volta ao normal sao logadas na hora).
INTERVALO_REPETIR_ALARME_S = 60.0
