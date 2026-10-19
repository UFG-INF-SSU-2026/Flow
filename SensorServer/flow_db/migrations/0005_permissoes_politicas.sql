-- 0005 (B1.6): permissoes por tabela (matriz do roadmap, secao 6.2 = flow_db/permissoes.py), compressao e retencao.
-- Exige os papeis (python -m flow_db provisionar). Nenhum servico apaga nas tabelas de fronteira (so sessoes).

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO flow_borda, flow_analise;

-- borda
GRANT SELECT, INSERT ON leituras, telemetria, auditoria              TO flow_borda;
GRANT INSERT ON respostas_dispositivo, entregas_push                 TO flow_borda;
GRANT SELECT ON comandos_push, alertas, confirmacoes, episodios, estado_dispositivo, cursor_analise TO flow_borda;
GRANT SELECT, INSERT, UPDATE ON cursor_borda, contas                 TO flow_borda;
GRANT SELECT, INSERT, DELETE ON sessoes                              TO flow_borda;
GRANT SELECT ON saude_pipeline                                       TO flow_borda;

-- analise
GRANT SELECT ON leituras, respostas_dispositivo, entregas_push       TO flow_analise;
GRANT SELECT, INSERT ON comandos_push, alertas, confirmacoes         TO flow_analise;
GRANT SELECT, INSERT, UPDATE ON episodios, estado_dispositivo, cursor_analise TO flow_analise;

-- ambos leem a versao do esquema (verificacao na subida)
GRANT SELECT ON schema_migrations TO flow_borda, flow_analise;

-- compressao (dados com mais de 7 dias; segmentados por usuario) e retencao
ALTER TABLE leituras SET (timescaledb.compress, timescaledb.compress_segmentby = 'user_id',
                          timescaledb.compress_orderby = 'instante_ingestao DESC');
ALTER TABLE telemetria SET (timescaledb.compress, timescaledb.compress_segmentby = 'user_id',
                            timescaledb.compress_orderby = 'instante DESC');
SELECT add_compression_policy('leituras',   INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_compression_policy('telemetria', INTERVAL '7 days', if_not_exists => TRUE);

SELECT add_retention_policy('leituras',              INTERVAL '90 days', if_not_exists => TRUE);
SELECT add_retention_policy('telemetria',            INTERVAL '90 days', if_not_exists => TRUE);
SELECT add_retention_policy('respostas_dispositivo', INTERVAL '90 days', if_not_exists => TRUE);
SELECT add_retention_policy('entregas_push',         INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_retention_policy('comandos_push',         INTERVAL '30 days', if_not_exists => TRUE);
-- episodios encerrados (1 ano) e alertas: tabelas comuns, sem politica automatica (limpeza fica para a parte 2).
