# Repositório
As informações de pastas/arquivos aqui citados vêm TODOS do repositório contido em: https://github.com/UFG-INF-SSU-2026/Flow

---

# Requisitos de Integração

**Disciplina:** Software para Sistemas Ubíquos
**Objetivo do marco:** produzir uma cooperação observável entre pelo menos dois componentes do grupo (produtor → comunicação → consumidor).

**Fronteira escolhida para este marco:** Wokwi (ESP32 + MPU6050, decisão de imobilidade) → `bridge.py` → servidor único (consumidor). A integração com o app Android (`leitura_ambiente`) fica fora do escopo deste relatório, por decisão do grupo — embora, no código, o mesmo servidor já receba as duas fontes (ver nota na decisão 2).

---

## 1. Cinco decisões que o grupo fechou antes de construir

1. **Componente que produz o primeiro evento:** o Wokwi (ESP32 simulado com MPU6050), via `sketch/sketch.ino`. Emite dois tipos de evento: `imobilidade.leitura` (heartbeat, 1x/s) e `imobilidade.decisao` (só em transições de estado).
2. **Componente que consome e produz efeito observável:** o servidor Flask único (`SensorServer/server.py`) recebe via `bridge.py`, confere a identificação do evento (`user_id` + `source`) e agrega a leitura mais recente de cada fonte numa janela de 1 segundo, gravando o log combinado por usuário. **Ainda não implementados:** validação de schema por `eventType`, uma tabela de estado persistente por usuário e a geração do artefato de alerta a partir de `imobilidade.decisao` — esses itens seguem como pendência (ver seção 2, Consumidor).
3. **Contrato mínimo e versionamento:** o que existe hoje é um envelope de **transporte** comum e cifrado (`{ nonce, ciphertext }`, AES-256-GCM), igual para as duas fontes. Dentro do texto decifrado **não há** um envelope de aplicação unificado (sem `schemaVersion`, sem um campo `payload` separando metadados do evento): cada fonte manda um JSON próprio, com apenas dois campos de roteamento em comum — `user_id` (inteiro) e `source` (`"app"` ou `"wokwi"`). Do lado do wokwi, os campos vêm como o firmware os emite: `eventType`, `deviceId`, `entityId`, `eventTimeMs` (relógio interno do ESP32, `millis()` desde o boot — não é um timestamp absoluto), `sequence`, `value`, `unit`, `state`. Controle de versão (`schemaVersion`) e unificação do envelope de aplicação continuam como trabalho futuro.
4. **Mecanismo:** HTTP, endpoint único (`POST /dados`), compartilhado pelas duas fontes, com um dispatcher interno por **`source`** (app vs. wokwi) — e não ainda por `eventType`, que seria o próximo refinamento natural do consumidor. Decisão baseada na pergunta-guia "consulta ou ação explícita" (ação explícita: registrar leitura/decisão de um dispositivo).
5. **Quem faz o quê:** a definir internamente pelo grupo (não é objeto deste relatório de arquitetura).

**Perguntas-guia da decisão 4, respondidas:**
- Múltiplos consumidores? Não — um único servidor consome. Pub/sub descartado.
- Consulta ou ação explícita? Ação explícita (ingestão de evento) → API baseada em recurso (HTTP), confirmado.
- Precisa operar sem rede? **Ainda não coberto:** o `bridge.py` atual apenas registra no console a falha de um POST e segue para o próximo evento lido da serial — não há fila de reenvio. A única tolerância a falha já implementada é a reconexão da porta serial RFC2217 quando a simulação Wokwi cai. Uma fila curta de reenvio para falhas transitórias de rede continua como item pendente.
- Comando produz consequência? **Parcialmente.** O firmware já evita reemitir o mesmo alerta (variável `alerta_ativo` na máquina de estados de `sketch.ino`) enquanto a condição de imobilidade persistir — isso é real e funciona hoje. Do lado do servidor, porém, `imobilidade.decisao` com `state = ALERTA_IMOBILIDADE` ainda **não** dispara nenhuma ação diferenciada: o evento é salvo dentro da chave `"wokwi"` do jsonl do usuário como qualquer outro evento.

---

## 2. Requisitos — Código Funcional

### Produtor
- [x] Derivado de protótipo anterior, reaproveitando decisões válidas: histerese de dois limiares (`LIMIAR_BAIXO`/`LIMIAR_ALTO`), persistência temporal (`T_PERSISTENCIA_MS`, `T_CONFIRMACAO_MS`), debounce nos botões, falha explícita (`LEITURA_INVALIDA`, `LEITURA_FORA_DE_FAIXA`), logs via `Serial.println` de cada evento estruturado.
- [x] Produz evento identificável: `sequence` incremental por `deviceId`, válido para qualquer `eventType` emitido.

### Comunicação
- [x] Transporta o envelope cifrado (AES-256-GCM) definido na decisão 3 — implementado em `bridge.py` (`encrypt_payload`, mesma chave e mesmo formato usados pelo app Android).
- [x] Torna observável ao menos uma condição de falha — Uma falha de POST gera, no servidor, valores null. Ou seja, o servidor, para falhas de post, não quebra por conta da falha, mas resiste e registra.
- [x] Usa o mecanismo definido na decisão 4 (HTTP, `POST /dados`).

### Consumidor
- [ ] Recebe e valida o evento/comando recebido — **implementado parcialmente:** o servidor hoje decifra o envelope e exige apenas que `source` esteja em `{"app", "wokwi"}` e que `user_id` seja um inteiro válido; requisições fora disso recebem `400`. **Ainda não implementada** a validação em cascata específica por `eventType` planejada: (1) `eventType` pertence ao vocabulário conhecido (`imobilidade.leitura`, `imobilidade.decisao`); (2) `state` pertence ao enum esperado daquele `eventType`; (3) `value` dentro da faixa física plausível (0–30 m/s²); (4) `sequence` maior que o último conhecido para aquele `deviceId` (dedup/idempotência). Essa cascata existe hoje só para o evento `leitura_ambiente` do app (`regra_luz.py`), não para os eventos do wokwi.
- [ ] Produz mudança de estado, log, interface ou atuação observável — **status real dos três canais:**
  - **Log — feito:** cada leitura recebida é guardada num buffer em memória por `user_id`, e a cada 1 segundo o servidor grava uma linha em `dataUsers/usuario_<user_id>.jsonl` (nome real do arquivo — não `user_<userId>.jsonl`) combinando a leitura mais recente de `app` e de `wokwi` naquela janela; a fonte que não enviou nada no segundo fica `null`. Isso significa que o log é **por janela de 1s, não por evento validado individualmente** — se o wokwi mandar dois `imobilidade.leitura` na mesma janela, só o mais recente é gravado.
  - **Mudança de estado — pendente:** não existe hoje uma tabela persistente `estado_por_usuario[userId]`. O que existe é só o buffer de agregação citado acima, que é limpo a cada flush de 1s (não guarda histórico de estado entre janelas).
  - **Atuação observável — pendente para o wokwi:** ainda não há, no servidor, nenhuma reação a `imobilidade.decisao` com `state = ALERTA_IMOBILIDADE`. Já existe um precedente funcional equivalente para a regra de luz do app (`alerta_<device_id>_<timestamp>.json`, gerado em `regra_luz.py` a partir de `leitura_ambiente`), que deve servir de modelo para estender a mesma lógica de alerta às decisões do wokwi.

---