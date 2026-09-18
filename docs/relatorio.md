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
2. **Componente que consome e produz efeito observável:** o servidor Flask único (`SensorServer/server.py`) recebe via `bridge.py`, confere a identificação do evento (`user_id` + `source`) e agrega a leitura mais recente de cada fonte numa janela de 1 segundo, gravando o log combinado por usuário. Além disso, para os eventos do wokwi, o servidor agora valida por `eventType` (módulo `regra_imobilidade.py`), mantém uma tabela de estado persistente por `deviceId` e gera o artefato de alerta a partir de `imobilidade.decisao` com `state = ALERTA_IMOBILIDADE` — ver seção 2, Consumidor.
3. **Contrato mínimo e versionamento:** o que existe hoje é um envelope de **transporte** comum e cifrado (`{ nonce, ciphertext }`, AES-256-GCM), igual para as duas fontes. Dentro do texto decifrado **não há** um envelope de aplicação unificado (sem `schemaVersion`, sem um campo `payload` separando metadados do evento): cada fonte manda um JSON próprio, com apenas dois campos de roteamento em comum — `user_id` (inteiro) e `source` (`"app"` ou `"wokwi"`). Do lado do wokwi, os campos vêm como o firmware os emite: `eventType`, `deviceId`, `entityId`, `eventTimeMs` (relógio interno do ESP32, `millis()` desde o boot — não é um timestamp absoluto), `sequence`, `value`, `unit`, `state`. Controle de versão (`schemaVersion`) e unificação do envelope de aplicação continuam como trabalho futuro.
4. **Mecanismo:** HTTP, endpoint único (`POST /dados`), compartilhado pelas duas fontes, com um dispatcher interno por **`source`** (app vs. wokwi) — e não ainda por `eventType`, que seria o próximo refinamento natural do consumidor. Decisão baseada na pergunta-guia "consulta ou ação explícita" (ação explícita: registrar leitura/decisão de um dispositivo).
5. **Quem faz o quê:** a definir internamente pelo grupo (não é objeto deste relatório de arquitetura).

**Perguntas-guia da decisão 4, respondidas:**
- Múltiplos consumidores? Não — um único servidor consome. Pub/sub descartado.
- Consulta ou ação explícita? Ação explícita (ingestão de evento) → API baseada em recurso (HTTP), confirmado.
- Precisa operar sem rede? **Ainda não coberto:** o `bridge.py` atual apenas registra no console a falha de um POST e segue para o próximo evento lido da serial — não há fila de reenvio. A única tolerância a falha já implementada é a reconexão da porta serial RFC2217 quando a simulação Wokwi cai. Uma fila curta de reenvio para falhas transitórias de rede continua como item pendente.
- Comando produz consequência? **Sim, nos dois lados agora.** O firmware já evitava reemitir o mesmo alerta (variável `alerta_ativo` na máquina de estados de `sketch.ino`) enquanto a condição de imobilidade persistir. Do lado do servidor, `imobilidade.decisao` com `state = ALERTA_IMOBILIDADE` agora dispara uma ação diferenciada: além de ser salvo na chave `"wokwi"` do jsonl do usuário (como qualquer outro evento), o servidor grava um arquivo `alerta_imobilidade_<device_id>_<timestamp>.json` (`regra_imobilidade.py`, mesmo padrão do `alerta_<device_id>_<timestamp>.json` já usado por `regra_luz.py` para o app) e mantém um flag `alerta_ativo` por `deviceId`, que evita gerar um novo arquivo para o mesmo episódio até chegar um `state` de `ESTADOS_QUE_ENCERRAM_ALERTA` (`CONFIRMADO_OK`, `MOVIMENTO_RETOMADO` ou `REARMADO_MANUAL`) — o servidor não confia cegamente no produtor para isso, mesmo que o firmware já se comporte assim.

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
- [x] Recebe e valida o evento/comando recebido — o servidor decifra o envelope, exige `source` em `{"app", "wokwi"}` e `user_id` inteiro (`400` fora disso), como já acontecia. **Adicionado:** para eventos do wokwi, `SensorServer/regra_imobilidade.py` (`validar_evento`) aplica a cascata planejada, chamada de dentro de `_processar_evento_wokwi` em `server.py`: (1) `eventType` pertence ao vocabulário conhecido (`imobilidade.leitura`, `imobilidade.decisao`); (2) `state` pertence ao enum esperado daquele `eventType`; (3) `value` dentro da faixa física plausível (0–30 m/s²) — com duas exceções deliberadas, coerentes com o próprio firmware: `LEITURA_INVALIDA` exige o sentinela `-1.0` (leitura falhou, não há magnitude) e `LEITURA_FORA_DE_FAIXA` exige `value > 30` (é o evento que existe justamente para reportar a anomalia; validar contra 0–30 rejeitaria o próprio aviso); (4) `sequence` maior que o último conhecido para aquele `deviceId` (`eh_duplicado`, dedup/idempotência — `sequence` é um contador único por dispositivo no firmware, não por `eventType`). Evento inválido ou duplicado é descartado e logado no console do servidor, sem interromper a requisição HTTP (mesmo padrão de tolerância a falha já usado em `regra_luz.py`). Essa cascata continua existindo, em paralelo, para `leitura_ambiente` do app (`regra_luz.py`) — os dois módulos seguem a mesma estrutura (`validar_evento` / `eh_duplicado` / função de regra), mas não compartilham código, pois os contratos são diferentes.
- [x] Produz mudança de estado, log, interface ou atuação observável — **status real dos três canais:**
  - **Log — feito (sem mudança):** cada leitura recebida é guardada num buffer em memória por `user_id`, e a cada 1 segundo o servidor grava uma linha em `dataUsers/usuario_<user_id>.jsonl` combinando a leitura mais recente de `app` e de `wokwi` naquela janela; a fonte que não enviou nada no segundo fica `null`. Continua sendo um log **por janela de 1s, não por evento validado individualmente**.
  - **Mudança de estado — feito:** `regra_imobilidade.py` mantém `_estados: dict[deviceId, EstadoDispositivo]` em memória, **fora** do buffer de agregação (não é limpo a cada flush de 1s): guarda `estado_atual` (último `state` de `imobilidade.leitura`), `ultima_decisao` (último `state` de `imobilidade.decisao`), `alerta_ativo`, `ultimo_sequence` e `ultima_atualizacao`. Exposta via `GET /estado/<device_id>` (novo endpoint em `server.py`), para poder mostrar a mudança de estado ao vivo na demonstração, sem precisar abrir o `.jsonl`.
  - **Atuação observável — feito:** ao chegar `imobilidade.decisao` com `state = ALERTA_IMOBILIDADE` (e nenhum alerta já ativo para aquele `deviceId`), `_processar_evento_wokwi` grava `dataUsers/alerta_imobilidade_<device_id>_<timestamp>.json`, seguindo o mesmo padrão do `alerta_<device_id>_<timestamp>.json` gerado por `regra_luz.py` para o app.

**Pendências que continuam em aberto** (fora do escopo fechado para este marco, não bloqueiam a demonstração):
- A tabela de estado (`_estados`) é por `deviceId`, em memória do processo — reinicia se o servidor cair, e não é uma tabela `estado_por_usuario[userId]` agregando múltiplos dispositivos do mesmo idoso.
- Fila de reenvio no `bridge.py` para falhas transitórias de rede (ver decisão 4) continua não implementada.

---