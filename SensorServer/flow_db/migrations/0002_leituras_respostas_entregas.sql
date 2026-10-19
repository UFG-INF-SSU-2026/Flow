-- 0002 (B1.3): tabelas de FRONTEIRA da borda. Tempo = instante em que a borda aceitou o dado (relogio do servidor).
-- Hypertables: o id (identity) NAO e chave primaria (a chave de particao teria de fazer parte dela); serve de
-- cursor de leitura, sempre acompanhado de um limite de instante para nao varrer chunks antigos.

CREATE TABLE IF NOT EXISTS leituras (
    id               bigint GENERATED ALWAYS AS IDENTITY,
    instante_ingestao timestamptz NOT NULL,
    user_id          integer NOT NULL,
    device_id        text    NOT NULL,
    fonte            text    NOT NULL CHECK (fonte IN ('app', 'wokwi')),
    tipo_evento      text,
    schema_version   smallint NOT NULL,
    instante_origem  timestamptz,           -- relogio do dispositivo; NAO e confiavel, so informativo
    conta_id         text,                  -- identidade vinda do token
    payload          jsonb   NOT NULL
);
SELECT create_hypertable('leituras', 'instante_ingestao', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS leituras_id_idx ON leituras (id);
CREATE INDEX IF NOT EXISTS leituras_user_instante_idx ON leituras (user_id, instante_ingestao DESC);

CREATE TABLE IF NOT EXISTS respostas_dispositivo (
    id                bigint GENERATED ALWAYS AS IDENTITY,
    instante_ingestao timestamptz NOT NULL,
    user_id           integer NOT NULL,     -- da identidade do token, nunca do corpo
    device_id         text,
    fonte             text NOT NULL CHECK (fonte IN ('app', 'wokwi')),
    tipo              text NOT NULL,        -- push.ack | confirmacao.resposta
    message_id        text,
    schema_version    smallint NOT NULL,
    payload           jsonb NOT NULL
);
SELECT create_hypertable('respostas_dispositivo', 'instante_ingestao', chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS respostas_id_idx ON respostas_dispositivo (id);
CREATE INDEX IF NOT EXISTS respostas_message_idx ON respostas_dispositivo (message_id, instante_ingestao DESC);

CREATE TABLE IF NOT EXISTS entregas_push (
    id         bigint GENERATED ALWAYS AS IDENTITY,
    instante   timestamptz NOT NULL,
    comando_id bigint,                      -- referencia logica a comandos_push.id (opcional)
    message_id text,
    user_id    integer NOT NULL,
    device_id  text,                        -- vazio quando o envio e para todos os canais do usuario
    canal      text NOT NULL CHECK (canal IN ('app', 'wokwi')),
    resultado  text NOT NULL CHECK (resultado IN ('enviado', 'sem_conexao', 'expirado', 'nao_suportado', 'erro')),
    detalhe    text
);
SELECT create_hypertable('entregas_push', 'instante', chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS entregas_id_idx ON entregas_push (id);
CREATE INDEX IF NOT EXISTS entregas_message_idx ON entregas_push (message_id, instante DESC);
