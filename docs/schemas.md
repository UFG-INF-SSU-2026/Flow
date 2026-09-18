# Schemas — Flow (templates para validação)

Templates dos dois payloads que chegam ao servidor (`POST /dados`, dentro do envelope cifrado `{"nonce","ciphertext"}`).

---

## 1. Wokwi (ESP32 + acelerômetro MPU6050) → servidor

| Campo | Tipo | Regra |
| --- | --- | --- |
| `schemaVersion` | inteiro | `2` (se ausente, servidor assume `1`) |
| `eventType` | string | `imobilidade.leitura` \| `imobilidade.decisao` |
| `deviceId` | string | obrigatório |
| `entityId` | string | obrigatório |
| `eventTimeMs` | inteiro | ms desde o boot (`millis()`) |
| `sequence` | inteiro | crescente por `deviceId`; `<=` último visto = duplicado |
| `value` | número | m/s²; `0..30`, exceto os sentinelas abaixo |
| `unit` | string | `"m/s2"` |
| `state` | string | depende do `eventType` (abaixo) |

`state` válido por `eventType`:

| `eventType` | `state` |
| --- | --- |
| `imobilidade.leitura` | `NORMAL`, `AGUARDANDO_CONFIRMACAO`, `ALERTA_CONFIRMADO` |
| `imobilidade.decisao` | `SUSPEITA_IMOBILIDADE`, `CONFIRMADO_OK`, `MOVIMENTO_RETOMADO`, `ALERTA_IMOBILIDADE`, `REARMADO_MANUAL`, `LEITURA_INVALIDA`, `LEITURA_FORA_DE_FAIXA` |

Exceções de `value`: `LEITURA_INVALIDA` → exatamente `-1.0`; `LEITURA_FORA_DE_FAIXA` → `> 30`.

> O bridge acrescenta `"user_id": <int>` e `"source": "wokwi"` antes de cifrar; o servidor exige os dois.

---

## 2. App Android (Kotlin) → servidor

| Campo | Tipo | Regra |
| --- | --- | --- |
| `user_id` | inteiro | obrigatório |
| `source` | string | `"app"` |
| `device_id` | string | `ANDROID_ID` |
| `device_model` | string | `Build.MODEL` |
| `timestamp_envio` | inteiro | epoch ms |
| `sensores` | objeto | chave = tipo do sensor (string); valor = `{nome: string, valores: número[], timestamp_leitura: inteiro}`; pode ser `{}` |
| `leitura_ambiente` | objeto | **opcional** (ausente se não houver leitura válida) |

`leitura_ambiente`:

| Campo | Tipo | Regra |
| --- | --- | --- |
| `schemaVersion` | inteiro | `2` (se ausente, servidor assume `1`) |
| `device_id` | string | obrigatório |
| `event_time` | string | ISO 8601 com offset (`yyyy-MM-dd'T'HH:mm:ss.SSSXXX`) |
| `luminosidade` | número | lux, `>= 0` |
| `cct` | número | Kelvin, `1000..12000` |
| `seq_num` | inteiro | crescente por `device_id`; `<=` último visto = duplicado |
