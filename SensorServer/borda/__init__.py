"""
BORDA (Servidor 1) do SensorServer: unico servico com porta de rede e com acesso ao banco, aos dispositivos, aos segredos de
transporte (chave AES, segredo JWT) e as contas. NAO decide nada: aceita, valida, grava, pergunta a analise e entrega.

  server.py          rotas HTTP, fila e descarga de leituras, envio a analise, threads de fundo
  auth.py, authz.py  contas, login/refresh, autorizacao em dois estagios
  crypto_utils.py    envelope AES-GCM
  validacao.py       validacao ESTRUTURAL dos corpos (contratos de schemas/)
  canal_*.py, sse_hub.py   canais de push (SSE para o app e para o bridge)
  cliente_analise.py chama a API da analise (HTTP local), detecta que ela reiniciou e manda o estado de volta (/restaurar)
  efeitos.py         grava as decisoes da analise numa transacao e, depois do commit, entrega o push
  tempo.py           relogio do ponto de entrada (instante da ingestao)
  servico.py, __main__.py   a borda como processo proprio (`python -m borda`), com desligamento ordenado

Regra de fronteira (conferida por tests/unit/test_fronteiras.py): nenhum modulo daqui importa `analise`. A borda nunca
importa regras; a analise e outro processo e a borda a chama por HTTP.
"""
