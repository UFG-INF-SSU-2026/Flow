# Instruções — Flow (SensorServer + SensorApp + Wokwi + WokwiBridge)

Este guia mostra como colocar os quatro componentes do projeto para rodar
juntos: o servidor, o app Android, a simulação Wokwi (detecção de
imobilidade) e a ponte que liga a simulação ao servidor.

## Pré-requisitos

- Python 3.10 ou superior, com `pip`.
- Um celular Android para instalar o app.
- VS Code com a extensão **Wokwi for VS Code** instalada, para rodar a
  simulação em `Sketch/`.
- Todos os componentes (servidor, celular e computador que roda a
  simulação/bridge) na mesma rede local.

## 1. Subir o servidor

```bash
cd SensorServer
pip install -r requirements.txt
python server.py
```

O servidor sobe em `0.0.0.0:5000`, aceitando conexões de qualquer
dispositivo na mesma rede. Anote o IP do computador onde ele está rodando
(`ipconfig` no Windows, `ifconfig` ou `ip a` no Linux/Mac) — vai ser usado
tanto no app quanto no bridge.

O log do servidor mostra cada leitura recebida e, a cada segundo, a linha
gravada em `dataUsers/usuario_<user_id>.jsonl` para cada usuário que teve
alguma leitura nova.

## 2. Instalar e configurar o app Android

1. Baixe o APK na página de *releases* do repositório no GitHub e instale
   no celular do idoso.
2. Abra o app e toque em **Configurações**.
3. Preencha:
   - **Endereço IP do servidor** e **Porta** (o IP anotado no passo 1,
     porta `5000`).
   - **ID do usuário**: um número inteiro que identifica o idoso. Se este
     mesmo idoso também estiver associado a um wokwi, use o mesmo número
     configurado no bridge (passo 4).
4. Toque em **Salvar** e depois em **Iniciar** para começar a enviar
   leituras a cada segundo.

## 3. Rodar a simulação Wokwi

1. Abra a pasta `Sketch/` no VS Code (com a extensão Wokwi instalada).
2. Pressione `F1` e execute **Wokwi: Start Simulator**.
3. Deixe a aba do simulador visível — a extensão pausa a simulação quando
   ela sai de foco/fica escondida.

A simulação representa um dispositivo vestível (ESP32 + acelerômetro
MPU6050) que detecta possível imobilidade do idoso, emitindo um evento de
leitura a cada segundo e um evento de decisão sempre que o estado muda.

## 4. Rodar a ponte (bridge)

```bash
cd WokwiBridge
pip install -r requirements.txt
```

Antes de rodar, abra `bridge.py` e confira/ajuste as constantes no topo do
arquivo:

```python
WOKWI_USER_ID = 1                                # mesmo ID configurado no app, se for o mesmo idoso
SERVIDOR_URL = "http://<IP_DO_SERVIDOR>:5000/dados"
```

Depois, com a simulação já rodando (passo 3):

```bash
python bridge.py
```

O bridge conecta na porta serial exposta pela simulação, lê cada evento,
anexa `WOKWI_USER_ID`, cifra e envia ao servidor. A saída do terminal
mostra cada evento capturado e a resposta do servidor.

## 5. Conferir os dados recebidos

Com o servidor, o app e o bridge rodando (usando o mesmo `user_id` no app
e no bridge, se representarem o mesmo idoso), verifique o arquivo gerado:

```bash
cat SensorServer/dataUsers/usuario_1.jsonl
```

Cada linha representa uma janela de 1 segundo, com a leitura mais recente
de cada fonte naquele intervalo — ou `null` na fonte que não enviou nada
naquele segundo.

Se a leitura de luminosidade do app disparar a regra de alerta, um arquivo
adicional aparece na mesma pasta:

```
dataUsers/alerta_<device_id>_<timestamp>.json
```

## 6. Encerrando

- No app: toque em **Parar**.
- No bridge e no servidor: `Ctrl+C` no terminal onde cada um está rodando.
- Na simulação: pare a simulação pelo VS Code (`F1` → `Wokwi: Stop
  Simulator` ou feche a aba).
