"""
Migracao minima de que cada servico depende. Ao subir, o servico apenas VERIFICA a versao aplicada
(conexao.verificar_versao_minima) e se recusa a subir se faltar: quem migra e o comando proprio
`python -m flow_db migrar`.

Ao criar uma migracao nova da qual o codigo passa a depender, suba o numero aqui. Um teste unitario garante que
o valor nunca ultrapassa a ultima migracao disponivel em flow_db/migrations/.
"""
MIGRACAO_MINIMA = {
    "borda": 5,
    "analise": 5,
}
