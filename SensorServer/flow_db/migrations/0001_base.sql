-- 0001: esquema do Flow. PostgreSQL comum, exceto "leituras", que e uma hypertable do TimescaleDB (a unica); sem
-- compressao, retencao nem agregados continuos.
-- Quem escreve em todas elas e a borda (Servidor 1); a analise nao toca no banco.
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Uma linha por leitura aceita (instante_ingestao = relogio do servidor ao aceitar o dado).
-- A coluna de tempo faz parte da chave primaria: o TimescaleDB exige isso em toda chave unica de uma hypertable.
CREATE TABLE leituras (
    id                bigint GENERATED ALWAYS AS IDENTITY,
    instante_ingestao timestamptz NOT NULL,
    user_id           integer NOT NULL,
    device_id         text    NOT NULL,
    fonte             text    NOT NULL CHECK (fonte IN ('app', 'wokwi')),
    tipo_evento       text,
    schema_version    smallint NOT NULL,
    instante_origem   timestamptz,           -- relogio do dispositivo; NAO e confiavel, so informativo
    conta_id          text,                  -- identidade vinda do token
    payload           jsonb   NOT NULL,
    PRIMARY KEY (id, instante_ingestao)
);
SELECT create_hypertable('leituras', 'instante_ingestao');
CREATE INDEX leituras_user_instante_idx ON leituras (user_id, instante_ingestao DESC);
CREATE INDEX leituras_instante_idx ON leituras (instante_ingestao DESC);

CREATE TABLE alertas (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    instante          timestamptz NOT NULL,
    user_id           integer NOT NULL,
    device_id         text,
    tipo              text NOT NULL,
    mensagem          text,
    dados             jsonb NOT NULL DEFAULT '{}',
    episodio_id       text,
    schema_version    smallint
);
CREATE INDEX alertas_user_instante_idx ON alertas (user_id, instante DESC);

CREATE TABLE confirmacoes (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    instante          timestamptz NOT NULL,
    user_id           integer NOT NULL,
    tipo              text NOT NULL,
    dados             jsonb NOT NULL DEFAULT '{}',
    episodio_id       text,
    message_id        text,
    schema_version    smallint
);
CREATE INDEX confirmacoes_user_instante_idx ON confirmacoes (user_id, instante DESC);

CREATE TABLE episodios (
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
CREATE INDEX episodios_abertos_idx ON episodios (user_id) WHERE estado <> 'encerrado';

-- Historico das mensagens de push. O message_id e unico: regravar o mesmo comando nao duplica. E daqui que sai, ao
-- reiniciar, a ligacao entre a resposta do dispositivo e o episodio.
CREATE TABLE comandos_push (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    criado_em         timestamptz NOT NULL,
    user_id           integer NOT NULL,
    device_id         text,
    canal             text NOT NULL CHECK (canal IN ('app', 'wokwi', 'todos')),
    message_id        text NOT NULL UNIQUE,
    tipo              text NOT NULL,
    payload           jsonb NOT NULL,
    expira_em         timestamptz,
    episodio_id       text
);
CREATE INDEX comandos_episodio_idx ON comandos_push (episodio_id);

-- Resumo do estado de imobilidade de cada dispositivo (rota GET /estado/<device_id>).
CREATE TABLE estado_dispositivo (
    user_id       integer NOT NULL,
    device_id     text    NOT NULL,
    atualizado_em timestamptz NOT NULL,
    versao_regra  integer NOT NULL,
    estado        jsonb   NOT NULL,
    PRIMARY KEY (user_id, device_id)
);
