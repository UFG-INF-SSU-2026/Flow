-- 0002: retencao de `leituras` (a unica hypertable): o TimescaleDB apaga, em segundo plano, os pedacos (chunks) inteiramente
-- mais velhos que 14 dias. Como o apagamento e por chunk (7 dias por padrao), uma leitura pode ficar entre 14 e 21 dias.
-- O prazo tem de ser MAIOR que o atraso maximo tolerado (a restauracao da analise usa so os ultimos 30 s).
-- As demais tabelas que crescem (alertas, confirmacoes, episodios, comandos_push) sao limpas pela borda, de hora em hora
-- (borda/server.py, passo_retencao), com 90 dias; sem compressao nem agregados continuos.
-- Para mudar o prazo: nova migracao com remove_retention_policy('leituras') e um novo add_retention_policy.
SELECT add_retention_policy('leituras', INTERVAL '14 days', if_not_exists => TRUE);
