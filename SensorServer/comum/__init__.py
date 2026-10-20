"""
Pacote comum do SensorServer (usado pela borda e pela analise).

  schemas.py    carrega os contratos de SensorServer/schemas/ e valida valores contra eles
  contratos.py  funcoes de conveniencia sobre os contratos (validar por nome, vocabulario, versao)

A conexao, as migracoes e as interfaces de acesso ao banco continuam em flow_db/ (que tambem e comum aos dois
servicos). Este pacote NAO importa Flask nem nenhum modulo da borda ou da analise.
"""
