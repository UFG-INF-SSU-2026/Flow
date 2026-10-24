"""
ANALISE do SensorServer: decide (queda, imobilidade, luz) e nao tem porta de rede nem credencial de dispositivo.

  nucleo.py          nucleo das regras: "processar este lote com este relogio e este estado" (sem I/O, sem thread)
  regra_queda.py, regra_imobilidade.py, regra_luz.py   as regras, com o estado em memoria explicito
  efeitos.py         persistencia das decisoes do nucleo, dentro da transacao do ciclo (tabelas e arquivos da analise)
  laco.py            laco por cursor: le leituras e respostas do banco, chama o nucleo, grava decisoes e cursor,
                     recupera o estado na subida e roda o vigia de episodios
  servico.py, __main__.py   a analise como processo proprio (`python -m analise`), sem porta de rede

Regra de fronteira (conferida por tests/unit/test_fronteiras.py): nada aqui importa Flask nem `borda`. A analise
conversa com a borda so pelo banco.
"""
