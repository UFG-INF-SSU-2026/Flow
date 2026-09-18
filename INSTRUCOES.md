# Flow Unificado — Marco 3 (integração ubíqua)

Este projeto une o **Flow-main** (app Android + servidor) com a **branch**
(simulação Wokwi + bridge), formando um único servidor central capaz de
receber dados de **duas fontes diferentes** — o celular do idoso e um
dispositivo wokwi — associando-os ao **mesmo usuário** quando pertencem à
mesma pessoa.

## Estrutura

```adress
Flow-Unificado/
├── SensorServer/     # servidor único (Flask) - recebe dados do app E do wokwi
├── SensorApp/         # app Android (Kotlin) - envia dados do celular
├── WokwiBridge/        # ponte Python entre a simulação Wokwi e o servidor
├── Sketch/             # projeto Wokwi (ESP32 + MPU6050), simulado no VS Code
└── INSTRUCOES.md       # este arquivo
```

## O que mudou em relação aos projetos originais

1. **Identificação de usuário (`user_id`)**: tanto o app quanto o wokwi agora
   enviam um `user_id` inteiro. Dados de fontes diferentes com o **mesmo**
   `user_id` são entendidos como pertencentes à mesma pessoa e salvos juntos.
   - No app: configurável pelo botão **"Configurações"** (antigo "Socket de
     Rede"), junto com IP e porta do servidor. Persistido no celular.
   - No wokwi: configurável em `WokwiBridge/bridge.py`, constante
     `WOKWI_USER_ID` (não precisa reiniciar a simulação para trocar, só o
     bridge).
2. **Frequência de envio = 1 segundo** nas duas fontes (antes o app enviava a
   cada 10s). Isso permite ao servidor juntar leituras de app e wokwi na
   mesma janela de tempo.
3. **Criptografia também no wokwi**: o `bridge.py` agora cifra cada evento
   (AES-256-GCM, mesma chave e mesmo formato `{"nonce":..., "ciphertext":...}`
   usados pelo app) antes de enviar ao servidor, para manter compatibilidade
   com o único endpoint `/dados`, que só aceita payloads cifrados.
4. **Um arquivo `.jsonl` por usuário**: `SensorServer/dataUsers/usuario_<ID>.jsonl`.
   A cada 1 segundo, o servidor grava uma linha combinando o que chegou de
   cada fonte naquela janela:

   ```json
   {
     "timestamp": "2026-09-17T22:48:03.061Z",
     "user_id": 1,
     "app": { ... dados do celular ... },
     "wokwi": { ... dados do wokwi ... }
   }
   ```

   Se só uma das fontes enviou dado naquela janela, a outra chave fica
   `null`. Se nenhuma das duas enviou nada, nenhuma linha é gravada.
5. O fluxo de alerta já existente (`regra_luz.py` — luz fria à noite) continua
   funcionando normalmente para os dados do app, de forma independente da
   agregação acima.

## Como rodar

### 1. Servidor (`SensorServer/`)

```bash
cd SensorServer
pip install -r requirements.txt
python server.py
```

Escuta em `0.0.0.0:5000`. Descubra o IP do computador (`ipconfig` no Windows,
`ifconfig`/`ip a` no Mac/Linux) — vai precisar dele no app e no bridge.

### 2. App Android (`SensorApp/`)

Igual ao processo original (abrir no Android Studio, gerar APK — ver detalhes
no `README.md` herdado do Flow-main). Depois de instalar:

1. Abra o app, toque em **"Configurações"**.
2. Preencha o IP e a porta do servidor, e o **ID do usuário** (um número
   inteiro — combine esse número com quem for configurar o wokwi da mesma
   pessoa).
3. Toque em **"Iniciar"**.

### 3. Simulação Wokwi (`Sketch/`)

1. Abra a pasta `Sketch/` no VS Code com a extensão Wokwi instalada.
2. Inicie a simulação (`F1` → `Wokwi: Start Simulator`), deixando a aba do
   simulador visível.

### 4. Bridge (`WokwiBridge/`)

```bash
cd WokwiBridge
pip install -r requirements.txt
```

Antes de rodar, edite `bridge.py` e confirme/ajuste:

```python
WOKWI_USER_ID = 1          # mesmo ID configurado no app, se for o mesmo idoso
SERVIDOR_URL = "http://localhost:5000/dados"   # IP:porta do servidor
```

Depois:

```bash
python bridge.py
```

O bridge vai ler os eventos da simulação, cifrá-los e enviá-los ao servidor.

## Testando a agregação por usuário

Com o servidor, o app (ou um POST simulado) e o bridge rodando com o **mesmo
`user_id`**, confira o arquivo gerado:

```bash
cat SensorServer/dataUsers/usuario_1.jsonl
```

Cada linha deve trazer, a cada segundo, os dados mais recentes de `app` e de
`wokwi` daquele usuário (ou `null` na fonte que não enviou nada naquele
segundo).
