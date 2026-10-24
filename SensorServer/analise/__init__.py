"""
ANALISE do SensorServer (Servidor 2): decide (queda, imobilidade, luz). Nao tem banco, nao tem credencial de dispositivo e
nao entrega nada: a borda a chama por uma API HTTP local e grava e entrega o que ela decidir.

  nucleo.py          nucleo das regras: "processar este lote com este relogio e este estado" (sem I/O, sem thread)
  regra_queda.py, regra_imobilidade.py, regra_luz.py   as regras, com o estado em memoria explicito
  api.py             a API HTTP (/analisar, /vigiar, /restaurar, /saude) e o estado em memoria
  servico.py, __main__.py   a analise como processo proprio (`python -m analise`)

Regra de fronteira (conferida por tests/unit/test_fronteiras.py): nada aqui importa `borda` nem `flow_db`. O que a analise
e a borda partilham (tipos e JSON do contrato) mora em `comum/`.
"""
