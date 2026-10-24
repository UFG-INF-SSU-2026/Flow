"""
Pacote comum do SensorServer (usado pela borda e pela analise).

  schemas.py            carrega os contratos de SensorServer/schemas/ e valida valores contra eles
  contratos.py          funcoes de conveniencia sobre os contratos (validar por nome, vocabulario, versao)
  contrato_analise.py   tipos e JSON do que a borda envia a analise (Leitura, Resposta) e do que ela devolve (Decisoes)
  const.py              constantes comuns e carregador do .env (as da borda e as da analise ficam nos seus pacotes)
  mensagens.py          contrato da mensagem de push: quem produz (analise) e quem entrega (borda) usam o mesmo codigo

A interface do banco fica em flow_db/. Este pacote NAO importa Flask nem nenhum modulo da borda ou da analise.
"""
