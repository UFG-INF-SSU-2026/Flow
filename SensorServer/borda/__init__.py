"""
BORDA (gateway) do SensorServer: unico servico com porta de rede e com acesso aos dispositivos, aos segredos de
transporte (chave AES, segredo JWT) e as contas.

  server.py          rotas HTTP, ingestao e descarga de leituras, threads de fundo
  auth.py, authz.py  contas, login/refresh, autorizacao em dois estagios e auditoria
  crypto_utils.py    envelope AES-GCM
  validacao.py       validacao ESTRUTURAL dos corpos (contratos de schemas/)
  canal_*.py, sse_hub.py   entrega de push por SSE
  tempo.py           relogio do ponto de entrada (instante da ingestao)
  caminho_direto.py  UNICO modulo da borda que conhece a analise (S2 a S5: a borda chama a analise no mesmo processo)

Regra de fronteira (conferida por tests/unit/test_fronteiras.py): nenhum modulo daqui, exceto caminho_direto.py,
importa `analise`. A borda nunca importa regras.
"""
