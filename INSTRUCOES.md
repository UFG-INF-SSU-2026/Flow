# Flow Unificado — Marco 3 (integração ubíqua)

Este projeto une um app Android + servidor com uma simulação Wokwi + bridge, formando um único servidor central capaz de
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
├── INSTRUCOES.md       # este arquivo
└── README.md           # resumo do projeto
```

## Como rodar

Baixar e instalar o rduino-cli esp32:esp32:esp32 para compilar o wokwi

### 1. Servidor (`SensorServer/`)

```bash
cd SensorServer
pip install -r requirements.txt
python server.py
```

Escuta em `0.0.0.0:5000`. Descubra o IP do computador (`ipconfig` no Windows,
`ifconfig`/`ip a` no Mac/Linux) — vai precisar dele no app e no bridge.

### 2. App Android (`SensorApp/`)

Abrir no Android Studio, gerar APK — ver detalhes
no `README.md` herdado do Flow-main. Depois de instalar:

1. Abra o app, toque em **"Configurações"**.
2. Preencha o IP e a porta do servidor, e o **ID do usuário** (um número
   inteiro — combine esse número com quem for configurar o wokwi da mesma
   pessoa).
3. Toque em **"Iniciar"**.

### 3. Simulação Wokwi (`Sketch/`)

1. Abra a pasta `Sketch/` no VS Code com a extensão Wokwi instalada.
2. Execute o comando abaixo para compilar a aplicação wokwi: ```rduino-cli compile --fqbn esp32:esp32:esp32 --output-dir build .```
3. Inicie a simulação (`F1` → `Wokwi: Start Simulator`), deixando a aba do simulador visível.

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
