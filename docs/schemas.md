<!-- markdownlint-disable MD024 -->
# Schemas — Flow (templates para validação)

Documento único dos contratos que passam pelo servidor. Cada seção traz o schema **na sua versão atual** e, ao final, o **backlog de mudanças** daquele schema entre versões.

Cada schema evolui de forma independente: a versão atual de cada um fica na tabela abaixo e no cabeçalho da sua seção, e as regras gerais se referem sempre à "versão atual do schema", nunca a um número fixo.

## Versões atuais

| Seção | Schema | Versão atual (`schemaVersion`) |
| --- | --- | --- |
| 1 | Wokwi → servidor: telemetria | 3 |
| 2 | Wokwi → servidor: eventos de retorno | 3 |
| 3 | Servidor → firmware: comando serial | 3 |
| 4 | App Android → servidor: telemetria | 3 |
| 4.2 | App: `leitura_ambiente` (bloco aninhado, versão própria) | 3 |
| 5 | App Android → servidor: eventos de retorno | 3 |
| 6 | Servidor → dispositivo: push | 3 |
| 7 | Autenticação: login | 3 |
| 8 | Autenticação: refresh | 3 |

**Como evoluir um schema:** (1) altere a seção; (2) incremente a versão no cabeçalho da seção e nesta tabela; (3) adicione uma linha ao backlog daquela seção. Nada mais precisa mudar.

---

## 0. Convenções gerais

- **Envelope de transporte** (idêntico em todas as rotas e no push): `{"nonce": "<base64>", "ciphertext": "<base64>"}`, AES-256-GCM. Os schemas abaixo descrevem o **texto decifrado**.
- **Autenticação:** toda rota, exceto `/`, `/health`, `/auth/login` e `/auth/refresh`, exige `Authorization: Bearer <access_token>`.
- **`schemaVersion`** é obrigatório **na raiz** de todo payload de produtor e se refere à versão do schema daquele payload (ver tabela de versões atuais). Se ausente, o servidor assume `1`. A versão **mais recente** do contrato é definida num único lugar do servidor (`const.SCHEMA_VERSION_ATUAL`); `const.SCHEMA_VERSION_MINIMA` (hoje `1`) é a menor aceita.
  - Versão **de `1` até a mais recente** é aceita e **usada**, inclusive a anterior à atual (as regras logam o aviso "desatualizada"). Na regra de queda a versão decide a prioridade entre dispositivos do mesmo usuário (arquitetura, 9.2.1); wokwi v1/v2 (sem `valueMax`) alimentam a regra de forma degradada, e app v1/v2 (sem `acelerometro`) não geram leitura de aceleração.
  - Versão **maior que a mais recente**, menor que a mínima, não inteira (inclui `"3"` e `3.0`) ou booleana é **descartada e logada**. Em `/dados` e `/eventos` a resposta é `200` com `{"status": "ok", "descartado": true, "motivo": "schemaVersion_nao_suportado"}` e o conteúdo não chega ao buffer nem às regras. Em `/auth/login` e `/auth/refresh` a resposta é `400` (a ausência é aceita).
- **`source`** é obrigatório e deve ser `app` ou `wokwi` em `/dados` e `/eventos`; ausente ou inválido = `400`. **`user_id`** deve ser um número inteiro JSON; string (mesmo só de dígitos), booleano, decimal, `null` ou ausente = `400`. O corpo decifrado deve ser um objeto JSON; senão `400`.
- **`user_id` e `source`** continuam no payload de telemetria, mas **devem coincidir com as claims do token** (`user_ids`, `fonte`); divergência = `403`. O `device_id`/`deviceId` que o payload declarar também deve ser o `device_id` do token (o informado no login); divergência = `403`. O bridge faz login com o `deviceId` do firmware (`FLOW_BRIDGE_DEVICE`).
- **Tempos:** `issuedAt`/`expiresAt` em ISO 8601 UTC com milissegundos e `Z`. Prazos para o dispositivo são **relativos** (`timeoutMs`), nunca absolutos.

---

## 1. Wokwi (ESP32 + acelerômetro MPU6050) → servidor: telemetria

`POST /dados` · **Versão atual: 3**

| Campo | Tipo | Regra |
| --- | --- | --- |
| `schemaVersion` | inteiro | versão atual deste schema (se ausente, servidor assume `1`) |
| `eventType` | string | `imobilidade.leitura` ou `imobilidade.decisao` |
| `deviceId` | string | obrigatório |
| `entityId` | string | obrigatório |
| `eventTimeMs` | inteiro | ms desde o boot (`millis()`) |
| `sequence` | inteiro | crescente por `deviceId`; `<=` último visto = duplicado (lacunas são normais: os eventos da seção 2 consomem o mesmo contador) |
| `value` | número | m/s²; `0..160`, exceto os sentinelas abaixo |
| `valueMax` | número | opcional, só em `imobilidade.leitura`. Máximo de `value` amostrado no ciclo de 1 s (~50 ms); `value <= valueMax`; mesma faixa `0..160` |
| `unit` | string | `"m/s2"` |
| `state` | string | depende do `eventType` (abaixo) |

`state` válido por `eventType`:

| `eventType` | `state` |
| --- | --- |
| `imobilidade.leitura` | `NORMAL`, `AGUARDANDO_CONFIRMACAO`, `ALERTA_CONFIRMADO` |
| `imobilidade.decisao` | `SUSPEITA_IMOBILIDADE`, `CONFIRMADO_OK`, `MOVIMENTO_RETOMADO`, `ALERTA_IMOBILIDADE`, `REARMADO_MANUAL`, `LEITURA_INVALIDA`, `LEITURA_FORA_DE_FAIXA` |

Exceções de `value`: `LEITURA_INVALIDA` → exatamente `-1.0`; `LEITURA_FORA_DE_FAIXA` → `> 160`. Com o sensor em ±8 g a magnitude máxima mensurável é ≈136 m/s², então `LEITURA_FORA_DE_FAIXA` só ocorreria se a faixa do sensor fosse ampliada; o teto é mantido igual ao do app por consistência de contrato.

Versões 1 e 2 não trazem `valueMax`: a leitura entra na regra de queda com o pico igual a `value`, marcada como degradada.

> O bridge acrescenta `"user_id": <int>` e `"source": "wokwi"` antes de cifrar, **tanto na telemetria quanto nos eventos de retorno (seção 2)**. O `user_id` vem do login do bridge, não de configuração manual; o servidor confere ambos contra o token.

### Backlog de mudanças

| Versão | Mudança |
| --- | --- |
| v1 → v2 | `schemaVersion` adicionado (se ausente, o servidor assume `1`). |
| v2 → v3 | Novo campo opcional `valueMax` em `imobilidade.leitura`. Teto plausível de `value`/`valueMax` passa de `30` para **`160` m/s²** (MPU6050 em ±8 g); sentinela `LEITURA_FORA_DE_FAIXA` passa de `> 30` para `> 160`. `sequence` passa a compartilhar o contador com os eventos da seção 2 (lacunas são normais). `user_id`/`source` acrescentados pelo bridge passam a ser conferidos contra o token; `user_id` vem do login do bridge. |

---

## 2. Wokwi → servidor: eventos de retorno

`POST /eventos` · **Versão atual: 3**

Emitidos pelo firmware na serial como linhas JSON; o bridge os roteia para `/eventos`.

| Campo | Tipo | Regra |
| --- | --- | --- |
| `schemaVersion` | inteiro | versão atual deste schema |
| `eventType` | string | `push.ack` ou `confirmacao.resposta` |
| `deviceId` | string | obrigatório |
| `entityId` | string | obrigatório |
| `eventTimeMs` | inteiro | ms desde o boot |
| `sequence` | inteiro | mesmo contador da seção 1 |
| `messageId` | string | `messageId` da mensagem de push a que se refere |
| `state` | string | depende do `eventType` (abaixo) |
| `user_id` | inteiro | acrescentado pelo bridge (ver nota da seção 1); deve estar em `token.user_ids` |
| `source` | string | acrescentado pelo bridge; `"wokwi"` |

| `eventType` | `state` | Significado |
| --- | --- | --- |
| `push.ack` | `ATIVADO` | comando recebido e buzzer ligado |
| `push.ack` | `REJEITADO` | comando inválido (ex.: `timeoutMs` fora de `0 < t <= BUZZER_MAX_MS`) |
| `confirmacao.resposta` | `ESTOU_BEM` | botão pressionado com buzzer ativo |
| `confirmacao.resposta` | `SEM_RESPOSTA` | tempo esgotado sem botão |

### Backlog de mudanças

| Versão | Mudança |
| --- | --- |
| v3 (criação) | Schema criado: eventos `push.ack` e `confirmacao.resposta` enviados a `POST /eventos`. |

---

## 3. Servidor → firmware: comando serial local

Via bridge, serial local, texto · **Versão atual: 3**

Não é JSON (o ESP32 não usa biblioteca JSON), portanto não carrega `schemaVersion`; a versão é a do contrato. Uma linha terminada em `\n`:

| Linha | Campos |
| --- | --- |
| `CMD;BUZZER_ON;<messageId>;<timeoutMs>` | `timeoutMs` inteiro, `0 < t <= BUZZER_MAX_MS` (teto do firmware, hoje 30 s); fora disso o firmware responde `push.ack` `REJEITADO` (seção 2) |
| `CMD;BUZZER_OFF;<messageId>;<motivo>` | `motivo`: `CONFIRMADO`, `EXPIRADO` ou `CANCELADO` |

### Backlog de mudanças

| Versão | Mudança |
| --- | --- |
| v3 (criação) | Contrato criado (comandos `BUZZER_ON` e `BUZZER_OFF`). |

---

## 4. App Android (Kotlin) → servidor: telemetria

`POST /dados` · **Versão atual: 3**

| Campo | Tipo | Regra |
| --- | --- | --- |
| `schemaVersion` | inteiro | versão atual deste schema (na raiz) |
| `user_id` | inteiro | obrigatório; deve estar em `token.user_ids` |
| `source` | string | `"app"`; deve coincidir com `token.fonte` |
| `device_id` | string | `ANDROID_ID` |
| `device_model` | string | `Build.MODEL` |
| `timestamp_envio` | inteiro | epoch ms |
| `sensores` | objeto | chave = tipo do sensor (string); valor = `{nome: string, valores: número[], timestamp_leitura: inteiro}`; pode ser `{}` |
| `acelerometro` | objeto | opcional (ausente se não houver leitura válida); ver 4.1 |
| `leitura_ambiente` | objeto | opcional (ausente se não houver leitura válida); ver 4.2 |

### 4.1 `acelerometro`

Bloco da telemetria do app (versionado pela raiz). Derivado do `TYPE_ACCELEROMETER`.

| Campo | Tipo | Regra |
| --- | --- | --- |
| `magnitude` | número | m/s², norma `sqrt(x²+y²+z²)` da última amostra; `0..160` (inclui gravidade) |
| `magnitude_max_janela` | número | máximo da magnitude entre o envio anterior e este; `magnitude <= magnitude_max_janela <= 160` |
| `amostras` | inteiro | `>= 1`, amostras na janela |
| `janela_ms` | inteiro | `> 0`, duração da janela |

**Deduplicação:** o servidor descarta a leitura de aceleração cujo `timestamp_envio` (epoch ms, raiz do payload) seja menor ou igual ao último aceito para `(user_id, source, device_id)`. Se o valor regredir mais de 60 s (`TOLERANCIA_RELOGIO_S`), considera-se reajuste de relógio do celular e o valor passa a ser a nova referência. Payload sem `acelerometro` aproveitável ou sem `timestamp_envio` numérico não é duplicata: simplesmente não contribui.

### Backlog de mudanças (telemetria do app, incluindo `acelerometro`)

| Versão | Mudança |
| --- | --- |
| v1 → v2 | `schemaVersion` adicionado, apenas dentro de `leitura_ambiente` (ver 4.2). |
| v2 → v3 | `schemaVersion` passa a existir **na raiz** do payload. Novo bloco opcional `acelerometro` (seção 4.1). `user_id` deve estar em `token.user_ids` e `source` deve coincidir com `token.fonte`; divergência = `403`. |

### 4.2 `leitura_ambiente`

Bloco aninhado com **versão própria** · **Versão atual: 3**

| Campo | Tipo | Regra |
| --- | --- | --- |
| `schemaVersion` | inteiro | versão atual deste bloco (se ausente, servidor assume `1`) |
| `device_id` | string | obrigatório |
| `event_time` | string | ISO 8601 com offset (`yyyy-MM-dd'T'HH:mm:ss.SSSXXX`) |
| `luminosidade` | número | lux, `>= 0` |
| `cct` | número | Kelvin, `1000..12000` |
| `seq_num` | inteiro | crescente por `device_id`; `<=` último visto = duplicado |

#### Backlog de mudanças

| Versão | Mudança |
| --- | --- |
| v1 → v2 | `schemaVersion` adicionado (se ausente, o servidor assume `1`). |
| v2 → v3 | Versão do bloco passa de `2` para `3`, sem mudança de campos. |

---

## 5. App Android → servidor: eventos de retorno

`POST /eventos` · **Versão atual: 3**

| Campo | Tipo | Regra |
| --- | --- | --- |
| `schemaVersion` | inteiro | versão atual deste schema |
| `user_id` | inteiro | deve estar em `token.user_ids` |
| `source` | string | `"app"` |
| `device_id` | string | `ANDROID_ID` |
| `eventType` | string | `push.ack` ou `confirmacao.resposta` |
| `messageId` | string | mensagem de push a que se refere |
| `event_time` | string | ISO 8601 com offset |
| `state` | string | mesma tabela da seção 2 (`ATIVADO`/`REJEITADO`; `ESTOU_BEM`/`SEM_RESPOSTA`). Para o app, `ATIVADO` = alerta sonoro e botão exibidos |

### Backlog de mudanças

| Versão | Mudança |
| --- | --- |
| v3 (criação) | Schema criado: eventos `push.ack` e `confirmacao.resposta` enviados a `POST /eventos`. |

---

## 6. Servidor → dispositivo: push

SSE (`GET /push/app`, `GET /push/wokwi`) · **Versão atual: 3**

Cada evento SSE:

```json
id: <contador por usuário e canal>
event: push
data: {"nonce": "...", "ciphertext": "..."}
```

`id` é um inteiro crescente por usuário e canal. Parte de um valor derivado do instante de subida do servidor (em segundos), para nunca ficar atrás do `Last-Event-ID` guardado pelo cliente depois de um reinício; cabe em inteiro de 32 bits, e o cliente deve guardá-lo como tal ou maior. `Last-Event-ID` ausente, vazio, não numérico ou negativo vale `0`. Cada conexão tem fila pequena (16); cliente que não consome e a enche é desconectado e, ao reconectar com `Last-Event-ID`, recebe o que ainda não expirou.

Eventos SSE sem `data` cifrado: `event: reautenticar` (token a expirar; cliente renova e reconecta) e comentários `:keepalive` a cada 15 s.

Texto decifrado do `data`:

| Campo | Tipo | Regra |
| --- | --- | --- |
| `schemaVersion` | inteiro | versão atual deste schema |
| `messageId` | string | UUID v4; dispositivo ignora repetidos |
| `messageType` | string | `queda.confirmacao_solicitada` ou `queda.confirmacao_encerrada` |
| `userId` | inteiro | deve ser o do próprio dispositivo; senão descartar |
| `episodeId` | string | UUID v4 do episódio de queda |
| `issuedAt` | string | ISO 8601 UTC com `Z` |
| `expiresAt` | string | ISO 8601 UTC com `Z`; mensagem expirada não é entregue nem executada. Verificado pelo servidor (antes de enviar) e pela ponte do Wokwi; o app não depende de relógio absoluto e usa apenas `timeoutMs` |
| `payload` | objeto | depende de `messageType` |

| `messageType` | `payload` |
| --- | --- |
| `queda.confirmacao_solicitada` | `timeoutMs` (inteiro, `> 0`, default do servidor `5000`; **recalculado no reenvio**), `motivo` (string: `IMPACTO_SEGUIDO_DE_IMOBILIDADE` ou `IMOBILIDADE_PROLONGADA`, conforme o gatilho que abriu o episódio) |
| `queda.confirmacao_encerrada` | `resultado`: `CONFIRMADO`, `EXPIRADO` ou `CANCELADO` |

### Backlog de mudanças

| Versão | Mudança |
| --- | --- |
| v3 (criação) | Contrato de mensagem de push criado (SSE cifrado, `queda.confirmacao_solicitada` e `queda.confirmacao_encerrada`). |

---

## 7. Autenticação: login

`POST /auth/login` (corpo e resposta em envelope) · **Versão atual: 3**

Requisição:

| Campo | Tipo | Regra |
| --- | --- | --- |
| `schemaVersion` | inteiro | versão atual deste schema |
| `identificador` | string | id da conta |
| `segredo` | string | senha |
| `fonte` | string | fontes atuais: `app` e `wokwi` (uma nova fonte entra aqui); deve estar nas `fontes` permitidas da conta |
| `device_id` | string | identifica o dispositivo (vai para a claim do token); até 64 caracteres, senão `400` |

Resposta `200`:

| Campo | Tipo | Regra |
| --- | --- | --- |
| `access_token` | string | JWT HS256, validade padrão 1 h |
| `refresh_token` | string | opaco, uso único, validade padrão 7 dias |
| `expires_in` | inteiro | segundos de validade do `access_token` |
| `papel` | string | `idoso`, `dispositivo`, `admin` (futuro: `cuidador`); o cliente escolhe a interface por ele |
| `user_ids` | inteiro[] | pessoas monitoradas a que a conta se refere |

Claims do `access_token`: `sub`, `papel`, `user_ids`, `fonte`, `device_id`, `iat`, `exp`, `jti`.

Falhas: `401` credencial inválida (resposta idêntica para conta inexistente e senha errada); `429` bloqueio temporário por tentativas.

### Backlog de mudanças

| Versão | Mudança |
| --- | --- |
| v3 (criação) | Contrato de login criado. |

---

## 8. Autenticação: refresh

`POST /auth/refresh` (corpo e resposta em envelope) · **Versão atual: 3**

Requisição: `schemaVersion` (versão atual deste schema; ausente é aceita) e `refresh_token`. Resposta: mesma da seção 7. O refresh anterior é invalidado; reutilizá-lo devolve `401`.

### Backlog de mudanças

| Versão | Mudança |
| --- | --- |
| v3 (criação) | Contrato de refresh criado. |

---

## 9. Códigos de falha de autenticação/autorização

| HTTP | Quando |
| --- | --- |
| `401` | token ausente, malformado, expirado ou com assinatura inválida; credencial de login inválida |
| `400` | corpo que não é envelope ou não decifra para objeto JSON; `source` ausente ou inválido; `user_id` inválido; `device_id` inválido no login; `schemaVersion` inválido ou maior que a atual em login/refresh |
| `403` | papel sem a permissão da rota; `fonte` do token diferente da exigida; `user_id`/`source`/`device_id` do payload divergem do token |
| `429` | bloqueio temporário de login |

Corpo: `{"status": "erro", "codigo": "<texto curto>"}`. Falhas de conteúdo de evento já autenticado continuam descartadas internamente com resposta `200` (inclui `schemaVersion` não suportado em `/dados` e `/eventos`, com `descartado: true`). Toda negação `401`/`403` vai para `auditoria.jsonl`, rotacionado por tamanho.

### Backlog de mudanças

| Versão | Mudança |
| --- | --- |
| v3 (criação) | Códigos `401`, `403` e `429` e formato do corpo de erro criados junto com a autenticação. |

---

## 10. Registro de confirmações (saída do servidor)

`dataUsers/confirmacoes_usuario_<user_id>.jsonl` · uma linha JSON por ocorrência. É saída do servidor, não um contrato de produtor, e **não tem `schemaVersion`**.

Campos comuns: `timestamp` (ISO 8601 UTC) e `tipo`: `episodio.aberto`, `push.entrega`, `push.ack`, `confirmacao.resposta` ou `episodio.encerrado`. Toda linha traz `episodeId`.

| `tipo` | Campos próprios |
| --- | --- |
| `episodio.aberto` | `motivo` (`IMPACTO_SEGUIDO_DE_IMOBILIDADE` ou `IMOBILIDADE_PROLONGADA`). Só no gatilho A: `fonte_do_disparo` (fonte da leitura que fechou a condição), `versao_referencia`, `fontes_usadas` (lista de `{fonte, device_id, versao, degradada}`) e `fontes_desatualizadas` (lista de `{fonte, device_id, versao}`, vazia se não houve) |
| `push.entrega` | `messageId`, `messageType`, `canal`, `resultado`: `ENTREGUE_AO_CANAL`, `SEM_CANAL_ATIVO`, `TIPO_NAO_SUPORTADO` ou `ERRO_NO_CANAL` (o canal levantou exceção; os demais canais seguem) |
| `push.ack`, `confirmacao.resposta` | `eventType`, `state`, `messageId`, `device_id`, `source`. `episodeId` é o da `messageId`; `tardia: true` se o episódio já estava encerrado; `mensagem_desconhecida: true` (com `episodeId: null`) se a `messageId` é desconhecida ou de outro usuário |
| `episodio.encerrado` | `resultado`: `CONFIRMADO` ou `EXPIRADO` |

### Backlog de mudanças

| Versão | Mudança |
| --- | --- |
| sem versão | Campos de fusão no `episodio.aberto` do gatilho A; `tardia` para qualquer retorno de episódio encerrado; `mensagem_desconhecida`. |
