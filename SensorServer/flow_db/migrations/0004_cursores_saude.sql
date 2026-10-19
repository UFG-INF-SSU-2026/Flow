-- 0004 (B1.5): cursores de cada servico (uma linha cada) e visao de saude do pipeline.
-- fillfactor baixo: a linha e atualizada o tempo todo (batimento); deixa espaco para atualizacoes in-place (HOT).

CREATE TABLE IF NOT EXISTS cursor_analise (
    singleton              boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    ultimo_leitura_id      bigint NOT NULL DEFAULT 0,
    ultimo_leitura_instante timestamptz,
    ultimo_resposta_id     bigint NOT NULL DEFAULT 0,
    ultimo_entrega_id      bigint NOT NULL DEFAULT 0,
    atualizado_em          timestamptz NOT NULL DEFAULT now(),   -- batimento da analise
    versao_codigo          text,
    metricas               jsonb NOT NULL DEFAULT '{}'
) WITH (fillfactor = 50);
INSERT INTO cursor_analise (singleton) VALUES (true) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS cursor_borda (
    singleton               boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    ultimo_comando_id       bigint NOT NULL DEFAULT 0,
    ultimo_comando_instante timestamptz,
    atualizado_em           timestamptz NOT NULL DEFAULT now()
) WITH (fillfactor = 50);
INSERT INTO cursor_borda (singleton) VALUES (true) ON CONFLICT DO NOTHING;

-- Saude do pipeline (o /health da borda a consulta). Com cursores recem-criados o atraso e o total de leituras.
-- A visao roda com os privilegios do dono; so a borda recebe SELECT nela (0005).
CREATE OR REPLACE VIEW saude_pipeline AS
SELECT
    COALESCE((SELECT max(id) FROM leituras), 0) - COALESCE(ca.ultimo_leitura_id, 0)      AS atraso_ids,
    EXTRACT(EPOCH FROM (now() - ca.atualizado_em))::double precision                      AS idade_batimento_analise_s,
    EXTRACT(EPOCH FROM (now() - cb.ultimo_comando_instante))::double precision            AS idade_ultimo_comando_entregue_s
FROM (SELECT 1) x
LEFT JOIN cursor_analise ca ON true
LEFT JOIN cursor_borda   cb ON true;
