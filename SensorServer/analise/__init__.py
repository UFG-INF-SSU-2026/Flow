"""
ANALISE do SensorServer: decide (queda, imobilidade, luz) e nao tem porta de rede nem credencial de dispositivo.

  nucleo.py          nucleo das regras: "processar este lote com este relogio e este estado" (sem I/O, sem thread)
  regra_queda.py, regra_imobilidade.py, regra_luz.py   as regras, com o estado em memoria explicito
  efeitos.py         persistencia das decisoes do nucleo (arquivos e tabelas da analise)
  laco.py            laco por cursor (S3a): le leituras, respostas e entregas do banco e chama o nucleo (so log ate a S4)

Regra de fronteira (conferida por tests/unit/test_fronteiras.py): nada aqui importa Flask nem `borda`. A analise
conversa com a borda so pelo banco (ou, ate a S4, pelo adaptador borda/caminho_direto.py).
"""
