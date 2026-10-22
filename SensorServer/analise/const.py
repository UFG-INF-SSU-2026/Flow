"""
Constantes da ANALISE: limiares e prazos das regras (queda, imobilidade, luz) e o periodo do vigia.

Este modulo so importa `comum.const`, e apenas para que o .env ja esteja carregado. Quem usa importa o modulo
(`from analise import const as const_analise`) e le `const_analise.X` na hora do uso. As constantes comuns (versoes
dos contratos, prazos da mensagem de push, pasta de dados) estao em comum/const.py; as da borda, em borda/const.py.
Ver comum/const.py para as regras de uso.
"""
import os

from comum import const as _comum  # noqa: F401  (efeito colateral: o .env e carregado antes de qualquer leitura de ambiente)

# ===========================================================================
# 1. VIGIA E MEMORIA DAS REGRAS
# ===========================================================================

# Periodo (s) da thread vigia que encerra episodios vencidos. Define o atraso
# maximo entre o prazo do servidor e o envio do encerramento EXPIRADO.
INTERVALO_VIGIA_S = 1.0

# Teto de entradas das tabelas em memoria das regras de imobilidade e de luz: estados por (user_id, device_id), um
# teto para cada tabela. O device_id vem do cliente: ao estourar, os mais antigos saem. Valor bem acima do uso real
# (poucos usuarios e dispositivos); serve so para o consumo de memoria nao crescer sem limite.
MAX_ESTADOS_LEGADOS = 1_000

# ===========================================================================
# 2. FAIXA FISICA DA ACELERACAO
# ===========================================================================

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
# 3. DETECCAO DE QUEDA E CONFIRMACAO
# ===========================================================================
# Os prazos do pedido de confirmacao (TIMEOUT_CONFIRMACAO_S, MARGEM_SERVIDOR_S, TTL_ENCERRAMENTO_S) sao comuns:
# ver comum/const.py.

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
# 4. REGRA DE LUZ (MARCO 2)
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

# ===========================================================================
# 5. LACO DA ANALISE POR CURSOR (S3a do split)
# ===========================================================================

# Liga o laco que le `leituras`, `respostas_dispositivo` e `entregas_push` por cursor (thread do mesmo processo,
# papel flow_analise). FLOW_LACO_ANALISE=0 desliga. Na S3a o laco so registra as decisoes em log; quem age e o
# caminho direto.
LACO_ATIVO = os.environ.get("FLOW_LACO_ANALISE", "1") != "0"

# Espera (s) entre dois ciclos de leitura. E uma das tres esperas do orcamento de latencia (roadmap, secao 9): o pior
# caso soma esta espera. Se ainda houver dado para ler (lote cheio), o laco nao espera.
INTERVALO_LEITURA_S = 0.25

# Idade (s), contada do instante da ingestao, acima da qual uma leitura ou resposta so RECONSTROI o estado das regras
# e nao pode disparar acao (principio 4: agir apenas sobre o recente). Evita alertar com dado velho ao reprocessar
# apos uma parada. Na S3a so marca a decisao como "reconstrucao" no log e conta nas metricas; a S4 passa a obedecer.
# Valor inicial, a calibrar.
IDADE_MAX_ACAO_S = 30

# O vigia do laco usa o relogio real MENOS este atraso (s). Cobre o dado ainda em transito (descarga da borda +
# leitura da analise) para nao expirar um episodio cuja resposta ja chegou, mas ainda nao foi lida. Igual a soma
# das duas esperas que antecedem a analise (roadmap, secao 9).
ATRASO_VIGIA_S = 0.5

# Falha repetida do laco (banco fora do ar, por exemplo): no maximo uma linha de log neste intervalo (s).
INTERVALO_LOG_FALHA_S = 10

# Identifica o codigo da analise no batimento (`cursor_analise.versao_codigo`).
VERSAO_CODIGO = "s3a-sombra"

# Idem, quando a analise AGE (comum.const.ACOES_PELO_BANCO, S4).
VERSAO_CODIGO_ACOES = "s4-acoes"

# ===========================================================================
# 6. COMPARACAO EM MODO SOMBRA (S3b do split)
# ===========================================================================

# Liga a comparacao: o caminho direto e o laco da analise entregam as suas decisoes a analise/sombra.py, que as grava
# em dataUsers/<SOMBRA_ARQUIVO> (uma linha JSON por decisao) para `python relatorio_sombra.py` comparar. Desligada por
# padrao (FLOW_SOMBRA=1 liga): so vale a pena enquanto a S3b e a janela de estabilidade da S5 estiverem em curso, e
# exige o laco ligado (FLOW_LACO_ANALISE=1). A S4/S5 removem esta secao junto com o caminho direto.
SOMBRA_ATIVA = os.environ.get("FLOW_SOMBRA", "0") == "1"

# Orcamento de latencia do roadmap (secao 9: 3 esperas de 250 ms). E a TOLERANCIA entre o instante de uma decisao
# do caminho direto e o da mesma decisao na sombra, e o limite do atraso (relogio de parede) com que a sombra decide
# depois do caminho direto. Para as decisoes do vigia (relogio real, nao o da ingestao) a tolerancia soma os
# periodos do vigia e do laco (ver analise/sombra.tolerancia_instante).
ORCAMENTO_LATENCIA_S = 0.75

# Teto de decisoes guardadas em memoria pela sombra (as mais antigas saem). O arquivo nao tem teto: e a fonte do relatorio.
SOMBRA_MAX_REGISTROS = 20_000

# Arquivo (dentro de comum.const.DATA_DIR) com as decisoes dos dois lados.
SOMBRA_ARQUIVO = "sombra_decisoes.jsonl"

# Arquivo (dentro de comum.const.DATA_DIR) em que `simular_cenarios.py` anota a janela de tempo de cada cenario, para
# `relatorio_sombra.py` separar as decisoes por cenario.
SOMBRA_MANIFESTO = "sombra_cenarios.json"
