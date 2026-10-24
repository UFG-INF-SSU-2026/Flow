"""
BORDA (gateway) do SensorServer: unico servico com porta de rede e com acesso aos dispositivos, aos segredos de
transporte (chave AES, segredo JWT) e as contas. NAO decide nada: aceita, valida, grava e entrega.

  server.py          rotas HTTP, ingestao e descarga de leituras, threads de fundo
  auth.py, authz.py  contas, login/refresh, autorizacao em dois estagios e auditoria
  crypto_utils.py    envelope AES-GCM
  validacao.py       validacao ESTRUTURAL dos corpos (contratos de schemas/)
  canal_*.py, sse_hub.py   canais de push (SSE para o app e para o bridge)
  entrega_push.py    le `comandos_push` por cursor, entrega aos canais e grava `entregas_push`
  via_banco.py       o que a rota precisa da analise: so o estado do dispositivo (le `estado_dispositivo`) e a entrega
  saude.py           /health: batimento e atraso da analise, lidos da visao `saude_pipeline` do banco
  tempo.py           relogio do ponto de entrada (instante da ingestao)
  servico.py, __main__.py   a borda como processo proprio (`python -m borda`), com desligamento ordenado

Regra de fronteira (conferida por tests/unit/test_fronteiras.py): nenhum modulo daqui importa `analise`. A borda nunca
importa regras; a analise e outro processo e o banco e o unico elo entre as duas.
"""
