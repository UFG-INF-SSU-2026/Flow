# Simulação Wokwi (ESP32): compilar e executar

O ESP32 do projeto é **simulado localmente**: o firmware é compilado na própria máquina com o `arduino-cli` e roda na
extensão Wokwi do VS Code. Para o ESP32 simulado alcançar o bridge no seu computador, a simulação usa o **gateway de rede
do Wokwi** (`wokwigw`), que também roda localmente.

```text
ESP32 simulado (VS Code) --WiFi simulado--> wokwigw (localhost:9011) --> bridge (https://host.wokwi.internal:5443) --> borda
```

## Dependências

| Item | Versão | Onde obter |
| --- | --- | --- |
| `arduino-cli` | atual | <https://arduino.github.io/arduino-cli/latest/installation/> |
| Core `esp32:esp32` (Espressif) | **3.x** (o buzzer usa `tone()`/`noTone()`) | índice da Espressif, instalado pelo `arduino-cli` (abaixo) |
| Biblioteca ArduinoJson | **7.4.2** | instalada pelo `arduino-cli` (abaixo) |
| VS Code + extensão **Wokwi Simulator** | atual | marketplace do VS Code; a extensão pede uma licença gratuita, ativada pelo comando `Wokwi: Request a New License` |
| `wokwigw` (gateway de rede do Wokwi, código aberto) | atual | <https://github.com/wokwi/wokwigw/releases> (binários para Windows, Linux e macOS) |

As demais bibliotecas do firmware (`WiFi`, `WiFiClientSecure`, `HTTPClient`, `Wire` e o mbedTLS) já vêm com o core.

## 1. Instalar o compilador e o core (uma vez)

```bash
# Linux/macOS (no Windows, use o instalador da página do arduino-cli)
curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh

arduino-cli config init
arduino-cli config add board_manager.additional_urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core update-index
arduino-cli core install esp32:esp32
arduino-cli core list                      # confira: esp32:esp32 3.x
arduino-cli lib install "ArduinoJson@7.4.2"
```

O script de instalação põe o `arduino-cli` em `./bin`; mova-o para uma pasta do `PATH` ou chame-o pelo caminho.

## 2. Arquivos locais (fora do Git)

O firmware não compila sem estes dois arquivos em `sketch/`:

| Arquivo | Como obter |
| --- | --- |
| `ca_flow.h` | Gerado por `SensorServer/scripts/gerar_certificado.py` (o `scripts/preparar.*` o chama). Só o certificado público da CA |
| `segredos.h` | Copie de `segredos.h.example` e preencha: `FLOW_IDENTIFICADOR` e `FLOW_SEGREDO` (a conta de papel `dispositivo`, por exemplo `bridge1`), `FLOW_CHAVE_AES_B64` (o valor de `SHARED_KEY_B64` em `SensorServer/borda/crypto_utils.py`) e `FLOW_BRIDGE_URL` (deixe `https://host.wokwi.internal:5443`; uma URL `http://` não compila) |

A conta do firmware precisa listar o `device_id` dele (`DEVICE_ID` em `sketch.ino`, `esp32-decisao-imobilidade-01`):
`--device-id esp32-decisao-imobilidade-01` no `criar-conta`. O certificado do bridge precisa listar `host.wokwi.internal`:
`python scripts/gerar_certificado.py --nome bridge --dns host.wokwi.internal`, em `SensorServer/`.

## 3. Compilar

Dentro de `sketch/`:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32 --output-dir build .
```

O resultado vai para `sketch/build/` (fora do Git): `sketch.ino.bin` e `sketch.ino.elf`, os arquivos que o
`wokwi.toml` aponta para a simulação. Compile de novo sempre que mudar `sketch.ino`, um dos `.h`, o `segredos.h` ou o
`ca_flow.h` (por exemplo, depois de recriar a CA com `--forcar-ca`).

## 4. Executar

1. **Servidor e bridge no ar** ([`INSTRUCOES.md`](../INSTRUCOES.md), seções 1 e 3.1).
2. **`wokwigw`:** baixe o binário do seu sistema nas releases, extraia e deixe rodando num terminal à parte:

   ```bash
   ./wokwigw            # Linux/macOS (antes: chmod +x wokwigw); no Windows: .\wokwigw.exe
   ```

   Ele escuta em `localhost:9011`, que é o que o `wokwi.toml` usa (`[net] gateway = "ws://localhost:9011"`). Sem ele, a
   simulação não alcança o computador. Dentro da simulação, o computador se chama `host.wokwi.internal`.
3. **Simulação:** abra a pasta `sketch/` no VS Code e rode `F1` → `Wokwi: Start Simulator`. Mantenha a aba do simulador
   **visível**: com ela oculta, a simulação pausa.

A ordem entre bridge e simulação é indiferente (o firmware tenta o login de novo a cada 5 s), mas o `wokwigw` precisa
estar de pé antes da simulação.

### O que esperar

O firmware entra no Wi-Fi simulado (`Wokwi-GUEST`), acerta o relógio por NTP e segue: login → chave e pedido de
certificado → certificado → online (telemetria a cada 1 s e push). No monitor serial aparecem só mensagens de diagnóstico
(`WiFi conectado`, `login aceito`, `certificado emitido`); nenhum token, chave, senha ou evento passa pela serial. Sem
certificado, nada é enviado às rotas protegidas; sem o horário certo, nada é enviado.

### O circuito (`diagram.json`)

| Peça | Pino | Função |
| --- | --- | --- |
| Acelerômetro MPU6050 | SDA D21, SCL D22 | Movimento (o firmware o configura em ±8 g) |
| Botão verde "Estou bem" | D25 | Responde ao pedido de confirmação e encerra a suspeita local |
| Botão azul "Rearme" | D26 | Encerra só o alerta local |
| LED amarelo "Suspeita" | D27 | A máquina local de imobilidade fora do normal |
| LED vermelho "Alerta" | D14 | O servidor pediu a confirmação |
| Buzzer | D13 | Toca enquanto o pedido de confirmação está aberto |

### Simular uma queda

No painel do MPU6050, leve **dois** eixos ao máximo ao mesmo tempo (por exemplo X e Y) e depois zere todos juntos,
deixando parado. O simulador limita cada eixo a 2 g (19,6 m/s²), abaixo do limiar de impacto (20 m/s²): por isso um eixo
só nunca conta como impacto. O buzzer toca e o LED vermelho acende; o botão "Estou bem" os desliga, e o fim do prazo
também. Sem impacto, ficar parado por tempo suficiente abre o pedido por imobilidade prolongada.

Medido numa execução: a telemetria fica espaçada em cerca de 1 s, e cada handshake TLS no ESP32 simulado leva cerca de
3,2 s (no login e depois de mais de 10 s sem chamadas, quando o bridge fecha a conexão ociosa).

## Problemas comuns

| Sintoma | Causa provável e solução |
| --- | --- |
| Compilação falha por `ca_flow.h` ou `segredos.h` | Faltam os arquivos locais (seção 2) |
| Compilação falha em `ArduinoJson.h` | `arduino-cli lib install "ArduinoJson@7.4.2"` |
| Erro de compilação em `tone()`/`noTone()` | Core abaixo de 3.x: `arduino-cli core upgrade esp32:esp32` |
| `esp32:esp32` não encontrado | Falta o índice da Espressif: `arduino-cli config add board_manager.additional_urls ...` e `arduino-cli core update-index` (seção 1) |
| A extensão não acha o firmware | Compile com `--output-dir build` dentro de `sketch/` (o `wokwi.toml` espera `build/sketch.ino.bin`) |
| `login nao aceito (HTTP -1)` | Confira, nesta ordem: `wokwigw` rodando; bridge no ar; certificado do bridge com `host.wokwi.internal`; `ca_flow.h` da CA atual; o aviso `sem horario (NTP)` |
| Login recusado com `403 device_nao_permitido` | A conta não lista o `DEVICE_ID` do firmware: recrie-a com `--device-id esp32-decisao-imobilidade-01` |
| `handshake com certificado falhou 3 vezes` | O bridge recusou o certificado (vencido, de outra CA ou CA de dispositivos recriada); o firmware pede outro sozinho |
| A simulação para de enviar | A aba do simulador ficou oculta |
| Telemetria chega, mas o buzzer nunca toca | Simulação pausada (aba oculta), firmware não recompilado depois de uma mudança, ou o pedido expirou antes de chegar (registrado em `confirmacoes`) |
| Se o `wokwigw` não alcança `127.0.0.1` | No `Bridge/.env`, `FLOW_BRIDGE_HOST=0.0.0.0`, e inclua o IP do computador no certificado do bridge (`--ip`) |

O teste da lógica do firmware no PC, sem Wokwi, está em `sketch/host_test/README.md`.
