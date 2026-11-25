# Flow: instalação e execução

Passo a passo para subir todos os componentes numa máquina: servidor (com o banco), app Android, bridge e a simulação
Wokwi. Os detalhes técnicos (certificados, variáveis, ajustes e problemas menos comuns) estão em
[`docs/operacao.md`](docs/operacao.md).

**Ordem:** servidor → contas → app. Para o ESP32: bridge → `wokwigw` → simulação.

## Pré-requisitos

| Item | Versão | Para quê |
| --- | --- | --- |
| Python | 3.10 ou superior (testado no 3.12) | servidor, bridge e testes |
| Docker com Compose | atual | banco (PostgreSQL 17 + TimescaleDB) |
| Celular Android ou emulador | Android 7 (API 24) ou superior | app |
| JDK 21 e Android SDK (API 34) | só para compilar o app | o APK pronto está nas [releases](https://github.com/UFG-INF-SSU-2026/Flow/releases/latest) |
| VS Code + extensão Wokwi, `arduino-cli` com o core `esp32:esp32` 3.x, ArduinoJson 7.4.2, `wokwigw` | só para o ESP32 simulado | firmware, compilado e simulado localmente ([`docs/wokwi.md`](docs/wokwi.md)) |

O celular e o computador precisam estar **na mesma rede** (Wi-Fi ou o roteamento do próprio celular), e a porta TCP
**5000** precisa estar liberada no firewall do computador (no Ubuntu: `sudo ufw allow 5000/tcp`).

## 1. Servidor

Todos os comandos desta seção rodam em `SensorServer/`.

### 1.1 Dependências e configuração (uma vez)

```bash
cd SensorServer
pip install -r requirements.txt
cp .env.example .env          # Windows: Copy-Item .env.example .env
```

Gere quatro segredos e coloque cada um no `.env`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"   # FLOW_JWT_SECRET
python -c "import secrets; print(secrets.token_urlsafe(24))"   # FLOW_ANALISE_TOKEN, FLOW_DB_PASSWORD e FLOW_DB_ADMIN_PASSWORD (um para cada)
```

O `.env` nunca vai para o Git. As demais variáveis têm padrão (lista completa em [`docs/operacao.md`](docs/operacao.md)).

### 1.2 Certificados e banco

```bash
scripts/preparar.sh           # Windows: scripts\preparar.ps1
```

Esse script, que pode ser repetido sem risco:

- cria a CA do projeto e os certificados em `certs/` (fora do Git), incluindo a **CA de dispositivos**, que assina os
  certificados de cliente;
- coloca o IP atual do computador no certificado da borda e o reemite sozinho quando o IP muda (outra rede);
- sobe o banco em Docker e aplica a migração.

Para usar o ESP32 simulado, emita também o certificado do bridge (uma vez):

```bash
python scripts/gerar_certificado.py --nome bridge --dns host.wokwi.internal
```

### 1.3 Contas (uma vez, com o banco no ar)

A senha de cada conta é pedida no terminal. As contas com o mesmo `--user-id` são a mesma pessoa: é isso que junta o
celular e o ESP32.

```bash
python -m borda.auth criar-conta --id idoso1    --papel idoso       --user-id 1 --fontes app
python -m borda.auth criar-conta --id bridge1   --papel dispositivo --user-id 1 --fontes wokwi --device-id esp32-decisao-imobilidade-01
python -m borda.auth criar-conta --id cuidador1 --papel cuidador    --user-id 1 --fontes app
python -m borda.auth criar-conta --id admin1    --papel admin       # opcional: leitura de depuração
```

### 1.4 Subir

```bash
scripts/tudo.sh               # borda + análise; Windows: scripts\tudo.ps1
```

Ao subir, a borda mostra o endereço para usar no app, por exemplo `No app: IP 192.168.0.10, porta 5000`. Se a rede
mudar, pare (Ctrl+C) e rode `scripts/preparar.sh` e `scripts/tudo.sh` de novo.

## 2. App Android

1. Instale o APK das [releases](https://github.com/UFG-INF-SSU-2026/Flow/releases/latest). Para compilar, veja
   [`docs/operacao.md`](docs/operacao.md#compilar-o-app).
2. Abra **Configurações** e preencha o **IP** e a **porta** que a borda mostrou, e o **usuário** e a **senha**. Ao salvar,
   o app busca sozinho o certificado da CA do servidor, faz o login e pede o seu certificado de cliente.
3. Com uma conta **idoso** (`idoso1`): toque em **Iniciar** e aceite as permissões de notificação e localização. O app
   passa a enviar os sensores e recebe os pedidos de confirmação, inclusive com a tela apagada.
4. Com uma conta **cuidador** (`cuidador1`): a tela do cuidador abre sozinha, com o estado e o histórico.

O botão **Testar alerta** aciona os avisos do celular (alarme, vibração, lanterna e voz) sem depender do servidor.

## 3. ESP32 simulado (Wokwi) e bridge

### 3.1 Bridge

```bash
cd Bridge
pip install -r requirements.txt
cp .env.example .env          # os padrões servem para tudo na mesma máquina
python bridge.py
```

### 3.2 Firmware

O firmware é compilado na sua máquina com o `arduino-cli` e simulado na extensão Wokwi do VS Code; o ESP32 simulado
alcança o bridge pelo gateway `wokwigw`. Instalação das dependências, detalhes e problemas: [`docs/wokwi.md`](docs/wokwi.md).

1. **Dependências (uma vez):** `arduino-cli` com o core `esp32:esp32` 3.x (índice da Espressif) e a biblioteca
   ArduinoJson 7.4.2, como em [`docs/wokwi.md`](docs/wokwi.md#1-instalar-o-compilador-e-o-core-uma-vez).
2. **`sketch/segredos.h`** (fora do Git): copie de `segredos.h.example` e preencha `FLOW_IDENTIFICADOR` e `FLOW_SEGREDO`
   (a conta `bridge1` e a senha dela) e `FLOW_CHAVE_AES_B64` (o valor de `SHARED_KEY_B64` em
   `SensorServer/borda/crypto_utils.py`). Deixe `FLOW_BRIDGE_URL` como está. O `sketch/ca_flow.h` já foi gerado pelo
   `scripts/preparar.sh`.
3. **Compilar**, dentro de `sketch/`:

   ```bash
   arduino-cli compile --fqbn esp32:esp32:esp32 --output-dir build .
   ```

4. **`wokwigw`:** baixe das releases (<https://github.com/wokwi/wokwigw/releases>) e deixe rodando num terminal
   (`./wokwigw`; no Windows, `.\wokwigw.exe`).
5. **Simular:** abra a pasta `sketch/` no VS Code e rode `F1` → `Wokwi: Start Simulator`. Mantenha a aba do simulador
   visível: com ela oculta, a simulação pausa.

No monitor serial devem aparecer `login aceito` e `certificado emitido`.

## 4. Conferir que está funcionando

```bash
curl --cacert SensorServer/certs/ca.crt https://localhost:5000/health
docker compose -f SensorServer/docker-compose.yml exec -T db psql -U flow -d flow -c "SELECT instante_ingestao, fonte FROM leituras ORDER BY id DESC LIMIT 5"
```

O `/health` deve mostrar `analise.estado = ok`, e a consulta deve mostrar leituras recentes de `app` e de `wokwi`.

**Simular uma queda:**

- **No celular:** com **Iniciar** ativo, faça um movimento brusco e deixe o aparelho parado. O alarme toca e aparece o
  botão **Estou bem** (prazo de 5 s).
- **No Wokwi:** leve dois eixos do acelerômetro ao máximo ao mesmo tempo e depois zere todos. O buzzer toca e o LED
  vermelho acende até o **Estou bem** ou o fim do prazo.
- **Sem celular nem Wokwi:** em `SensorServer/`, com o servidor no ar:

  ```bash
  FLOW_BRIDGE_ID=bridge1 FLOW_BRIDGE_SECRET=<senha> python simular_queda.py
  ```

O cuidador vê o episódio no estado e, depois, no histórico. Entre dois episódios da mesma pessoa há um intervalo
mínimo de 10 s.

## 5. Testes

```bash
cd SensorServer && python -m pytest                 # servidor; com o banco no ar: python -m pytest -m banco
cd Bridge && python -m pytest                       # bridge
cd testes_integrados && python -m pytest            # ponta a ponta (firmware simulado → bridge → borda)
cd SensorApp && ./gradlew test                      # app, na JVM (JDK 21 e Android SDK)
```

O teste do firmware no PC está em `sketch/host_test/README.md`. O que cada suíte cobre e os resultados:
[`docs/testes.md`](docs/testes.md).

## Problemas mais comuns

| Sintoma | O que fazer |
| --- | --- |
| A borda não sobe e reclama de segredo, certificado ou CA de dispositivos | Confira o `.env` (seção 1.1) e rode `scripts/preparar.sh` |
| O app mostra falha de conexão | IP ou porta errados, redes diferentes, ou firewall bloqueando a porta 5000 |
| O app mostra erro de certificado | O IP digitado não é o que a borda mostrou ao subir; rode `scripts/preparar.sh` e suba de novo |
| A borda não conecta ao banco depois de reemitir os certificados | `docker compose restart db` em `SensorServer/` |
| Login recusado com `429` | Várias senhas erradas seguidas: espere 15 minutos |
| O Wokwi mostra `login nao aceito (HTTP -1)` | `wokwigw` parado, bridge parado ou certificado do bridge sem `host.wokwi.internal` (seção 1.2) |
| O pedido de confirmação não abre de novo logo depois de um teste | Intervalo mínimo de 10 s entre episódios |

Lista completa em [`docs/operacao.md`](docs/operacao.md#problemas-comuns).
