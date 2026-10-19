-- 0001: tabelas "legadas" do servidor atual (o roadmap original pressupunha que ja existiam; aqui nascem do zero).
CREATE EXTENSION IF NOT EXISTS timescaledb;
-- telemetria: 1 linha por usuario por janela de agregacao (substitui dataUsers/usuario_<id>.jsonl). Hypertable.

CREATE TABLE IF NOT EXISTS telemetria (
    instante timestamptz NOT NULL,
    user_id  integer     NOT NULL,
    app      jsonb,
    wokwi    jsonb
);
SELECT create_hypertable('telemetria', 'instante', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS telemetria_user_instante_idx ON telemetria (user_id, instante DESC);

-- auditoria de negacoes (401/403/413...). Nunca guarda o conteudo da requisicao.
CREATE TABLE IF NOT EXISTS auditoria (
    id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    instante timestamptz NOT NULL DEFAULT now(),
    rota    text,
    status  integer NOT NULL,
    motivo  text    NOT NULL,
    conta   text,
    ip      text
);
CREATE INDEX IF NOT EXISTS auditoria_instante_idx ON auditoria (instante DESC);

-- contas e sessoes (tabelas criadas aqui; o codigo ainda usa contas.json/sessoes.json ate a migracao de auth)
CREATE TABLE IF NOT EXISTS contas (
    conta_id   text PRIMARY KEY,
    papel      text NOT NULL,
    user_ids   integer[] NOT NULL DEFAULT '{}',
    fontes     text[]    NOT NULL DEFAULT '{}',
    hash_senha text NOT NULL,
    criada_em  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS sessoes (
    hash_refresh text PRIMARY KEY,
    conta_id     text NOT NULL,
    fonte        text,
    device_id    text,
    expira_em    timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS sessoes_expira_idx ON sessoes (expira_em);

-- alertas e confirmacoes (colunas de correlacao entram na 0003)
CREATE TABLE IF NOT EXISTS alertas (
    instante  timestamptz NOT NULL DEFAULT now(),
    user_id   integer NOT NULL,
    device_id text,
    tipo      text NOT NULL,
    mensagem  text,
    dados     jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS alertas_user_instante_idx ON alertas (user_id, instante DESC);

CREATE TABLE IF NOT EXISTS confirmacoes (
    instante timestamptz NOT NULL DEFAULT now(),
    user_id  integer NOT NULL,
    tipo     text NOT NULL,
    dados    jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS confirmacoes_user_instante_idx ON confirmacoes (user_id, instante DESC);
