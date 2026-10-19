-- 0003 (B1.4): tabelas da ANALISE e correlacao nas tabelas legadas (somente colunas novas, nada e removido).

CREATE TABLE IF NOT EXISTS comandos_push (
    id               bigint GENERATED ALWAYS AS IDENTITY,
    criado_em        timestamptz NOT NULL,
    user_id          integer NOT NULL,
    device_id        text,                  -- destino; vazio = todos os canais do usuario
    canal            text NOT NULL CHECK (canal IN ('app', 'wokwi', 'todos')),
    message_id       text NOT NULL,
    tipo             text NOT NULL,
    payload          jsonb NOT NULL,
    expira_em        timestamptz,
    episodio_id      text,
    leitura_id_origem bigint
);
SELECT create_hypertable('comandos_push', 'criado_em', chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS comandos_id_idx ON comandos_push (id);
CREATE INDEX IF NOT EXISTS comandos_message_idx ON comandos_push (message_id, criado_em DESC);

CREATE TABLE IF NOT EXISTS episodios (
    episodio_id         text PRIMARY KEY,
    user_id             integer NOT NULL,
    device_id           text,
    tipo                text NOT NULL,
    estado              text NOT NULL CHECK (estado IN ('aberto', 'aguardando_confirmacao', 'encerrado')),
    aberto_em           timestamptz NOT NULL,
    atualizado_em       timestamptz NOT NULL,
    encerrado_em        timestamptz,
    motivo_encerramento text,
    dados               jsonb NOT NULL DEFAULT '{}'
);
-- reconstruir episodios abertos ao reiniciar a analise
CREATE INDEX IF NOT EXISTS episodios_abertos_idx ON episodios (user_id) WHERE estado <> 'encerrado';

CREATE TABLE IF NOT EXISTS estado_dispositivo (
    user_id       integer NOT NULL,
    device_id     text    NOT NULL,
    atualizado_em timestamptz NOT NULL,
    versao_regra  integer NOT NULL,
    estado        jsonb   NOT NULL,
    PRIMARY KEY (user_id, device_id)
);

-- correlacao: o id preenche as linhas ja existentes (IDENTITY) e as colunas novas nascem vazias
ALTER TABLE alertas ADD COLUMN IF NOT EXISTS id bigint GENERATED ALWAYS AS IDENTITY;
ALTER TABLE alertas ADD COLUMN IF NOT EXISTS episodio_id text;
ALTER TABLE alertas ADD COLUMN IF NOT EXISTS leitura_id_origem bigint;
ALTER TABLE alertas ADD COLUMN IF NOT EXISTS schema_version smallint;
CREATE UNIQUE INDEX IF NOT EXISTS alertas_id_idx ON alertas (id);

ALTER TABLE confirmacoes ADD COLUMN IF NOT EXISTS id bigint GENERATED ALWAYS AS IDENTITY;
ALTER TABLE confirmacoes ADD COLUMN IF NOT EXISTS episodio_id text;
ALTER TABLE confirmacoes ADD COLUMN IF NOT EXISTS message_id text;
ALTER TABLE confirmacoes ADD COLUMN IF NOT EXISTS leitura_id_origem bigint;
ALTER TABLE confirmacoes ADD COLUMN IF NOT EXISTS schema_version smallint;
CREATE UNIQUE INDEX IF NOT EXISTS confirmacoes_id_idx ON confirmacoes (id);
