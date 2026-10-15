# Flow — Instruções de execução (Marco 3)

Este projeto une um app Android, uma simulação Wokwi (ESP32 + acelerômetro + buzzer), uma ponte (bridge) e um servidor central. O servidor recebe telemetria das duas fontes, associa-as à **mesma pessoa** pela conta autenticada, analisa o acelerômetro e, se concluir que o idoso provavelmente caiu, **empurra (push) um pedido de confirmação** de volta aos dispositivos.

## Estrutura

```dir
Flow/
├── SensorServer/       # servidor central (Flask): autenticação, autorização, análise e push
├── SensorApp/          # app Android (Kotlin): telemetria + recebe o pedido de confirmação
├── WokwiBridge/        # ponte Python bidirecional entre a simulação Wokwi e o servidor
├── sketch/             # projeto Wokwi (ESP32 + MPU6050 + buzzer), simulado no VS Code
├── docs/               # arquitetura e schemas
├── INSTRUCOES.md       # este arquivo
└── README.md           # análise e resumo do projeto
```

## Como as peças se falam

- **Dispositivo → servidor:** `POST` (telemetria em `/dados`, ack e resposta do idoso em `/eventos`), sempre com token.
- **Servidor → dispositivo:** o **dispositivo abre** uma conexão SSE com o servidor e o servidor empurra mensagens por ela. Nenhum dispositivo escuta porta; por isso o único requisito de rede é que o celular e o bridge **alcancem o servidor**.
- **Wokwi:** o ESP32 simulado só fala serial. O bridge lê e escreve nessa serial (RFC2217, `localhost:4000`) e faz a ponte com o servidor.

## Pré-requisitos

| Item | Observação |
| --- | --- |
| Python 3.10 ou superior | servidor e bridge |
| `arduino-cli` com o core `esp32:esp32` **3.x** | compila o sketch (o buzzer usa `tone()`/`noTone()`). Confira a versão com `arduino-cli core list` |
| VS Code + extensão **Wokwi** | simulação local (não pelo site) |
| Android Studio | gerar o APK, ou usar o APK das releases |
| Computador e celular na **mesma rede Wi-Fi** | o celular acessa o IP do computador |

## 0. Segredo e contas (uma vez)

O servidor **não sobe** sem o segredo de assinatura dos tokens. Gere um e exporte antes de iniciar o servidor:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Linux/macOS:

```bash
export FLOW_JWT_SECRET="<valor gerado>"
```

Windows (PowerShell):

```powershell
$env:FLOW_JWT_SECRET="<valor gerado>"
```

Crie as duas contas do protótipo (a senha é pedida no terminal). Ambas apontam para a **mesma pessoa** (`--user-id 1`), e é isso que faz o servidor juntar app e wokwi:

```bash
cd SensorServer
pip install -r requirements.txt
python auth.py criar-conta --id idoso1  --papel idoso       --user-id 1 --fontes app
python auth.py criar-conta --id bridge1 --papel dispositivo --user-id 1 --fontes wokwi
```

> O ID do usuário **não é mais digitado** no app nem no bridge: ele vem da conta. Para simular outra pessoa, crie outro par de contas com outro `--user-id`.

Os arquivos `contas.json` e `sessoes.json` são criados em `SensorServer/` e não vão para o Git.

## 1. Servidor (`SensorServer/`)

Com `FLOW_JWT_SECRET` definido no mesmo terminal:

```bash
cd SensorServer
python server.py
```

Escuta em `0.0.0.0:5000`. Descubra o IP do computador (`ipconfig` no Windows, `ifconfig`/`ip a` no Mac/Linux): o celular vai precisar dele.

**Firewall:** libere a **entrada** na porta TCP 5000 no computador. O celular e o bridge conectam *no servidor*; nada conecta no celular.

## 2. Simulação Wokwi (`sketch/`)

1. Abra a pasta `sketch/` no VS Code com a extensão Wokwi instalada.
2. Compile (dentro de `sketch/`):

   ```bash
   arduino-cli compile --fqbn esp32:esp32:esp32 --output-dir build .
   ```

3. Inicie a simulação (`F1` → `Wokwi: Start Simulator`) e **deixe a aba do simulador visível**: com a aba oculta a simulação pausa e o firmware deixa de ler comandos.

O diagrama tem agora um **buzzer** (pino D13) além do acelerômetro, dos botões "Estou bem" e "Rearme" e dos LEDs. Qualquer alteração em `sketch.ino` ou `diagram.json` exige repetir a compilação e reiniciar a simulação.

## 3. Bridge (`WokwiBridge/`)

```bash
cd WokwiBridge
pip install -r requirements.txt
```

O bridge lê a configuração de variáveis de ambiente (nada de editar o `bridge.py`):

| Variável | Valor | Padrão |
| --- | --- | --- |
| `FLOW_BRIDGE_ID` | `bridge1` | — (obrigatória) |
| `FLOW_BRIDGE_SECRET` | senha da conta `bridge1` | — (obrigatória) |
| `FLOW_SERVIDOR` | URL base do servidor | `http://localhost:5000` |

Linux/macOS:

```bash
export FLOW_BRIDGE_ID=bridge1
export FLOW_BRIDGE_SECRET="<senha da conta bridge1>"
python bridge.py
```

Windows (PowerShell):

```powershell
$env:FLOW_BRIDGE_ID="bridge1"
$env:FLOW_BRIDGE_SECRET="<senha da conta bridge1>"
python bridge.py
```

O bridge faz login, abre o canal de push do servidor, repassa a telemetria da simulação e escreve na serial os comandos do buzzer. Se a simulação ainda não estiver de pé, ele fica tentando reconectar.

## 4. App Android (`SensorApp/`)

Abra no Android Studio e gere o APK (ou instale o das releases). Depois de instalado:

1. Toque em **Configurações**.
2. Preencha **IP** e **porta** do servidor, e o **usuário** (`idoso1`) e a **senha** da conta criada no passo 0. Salve.
3. Toque em **Iniciar** e **mantenha o app aberto e em primeiro plano**.

Observações importantes:

- O app só envia dados **e só recebe o pedido de confirmação** enquanto está aberto e em primeiro plano. Com a tela bloqueada ou o app em segundo plano, ele não é alertado (limitação conhecida, ver `docs/marco3-autenticacao-autorizacao-push.md`, seção 13.2).
- Este app aceita apenas contas do tipo `idoso`. Outros tipos de conta serão atendidos no futuro pelo mesmo login.

## Testando

### Autenticação

Sem token, o servidor recusa:

```bash
curl -i -X POST http://localhost:5000/dados -H "Content-Type: application/json" -d "{}"
```

Esperado: `401`. Tentativas negadas ficam em `SensorServer/dataUsers/auditoria.jsonl`.

### Agregação por usuário

Com servidor, app e bridge rodando, confira:

```bash
cat SensorServer/dataUsers/usuario_1.jsonl
```

Cada linha traz os dados mais recentes de `app` e de `wokwi` do usuário 1 (`null` na fonte que não enviou nada naquele segundo).

### Queda e confirmação — Wokwi

1. Com a simulação rodando, leve os sliders do acelerômetro a um valor alto (impacto) por um instante.
2. Zere todos os sliders e mantenha parado por alguns segundos.
3. O servidor deve concluir "queda provável" (impacto seguido de imobilidade) e o **buzzer toca**. Pressione **Estou bem** em até 5 s: o buzzer cala.
4. Se não pressionar, o buzzer cala ao fim do prazo.

Sem impacto algum, deixar tudo zerado por tempo suficiente também dispara o pedido, por **imobilidade prolongada**, mas com mais atraso (o gatilho de impacto tem preferência). O campo `motivo` em `confirmacoes_usuario_1.jsonl` mostra qual gatilho abriu o episódio.

### Queda e confirmação — App

Com o app em primeiro plano e **Iniciar** ativo, simule um impacto (movimento brusco no aparelho) e deixe-o imóvel. O app toca o alerta e mostra o botão **Estou bem**, que some após 5 s.

### O que o servidor guarda

```bash
cat SensorServer/dataUsers/confirmacoes_usuario_1.jsonl
```

Cada episódio deixa linhas de abertura, entrega do push por canal, ack dos dispositivos, resposta do idoso (`ESTOU_BEM` ou `SEM_RESPOSTA`) e encerramento. **O servidor apenas armazena a resposta**; o que fazer com ela ainda não foi definido.

Se app e wokwi estiverem ligados à mesma pessoa, o pedido toca nos dois ao mesmo tempo, e o primeiro "Estou bem" silencia o outro.

## Ajustes comuns

| O que ajustar | Onde |
| --- | --- |
| Tempo do botão "Estou bem" (padrão 5 s) | `TIMEOUT_CONFIRMACAO_S` em `SensorServer/const.py` (vale para app e wokwi, sem recompilar nada) |
| Limiares de impacto e de repouso | `LIMIAR_IMPACTO` e `LIMIAR_REPOUSO` em `SensorServer/const.py` (valores iniciais, a calibrar) |
| Validade dos tokens | `ACCESS_TTL_S` e `REFRESH_TTL_S` em `SensorServer/const.py` |
| Intervalo de envio do app | `SEND_INTERVAL_MS` em `MainActivity.kt` (padrão 1 s) |

## Problemas comuns

| Sintoma | Causa provável |
| --- | --- |
| Servidor não sobe e reclama de segredo | `FLOW_JWT_SECRET` não definido **no terminal em que o servidor foi iniciado** |
| App ou bridge recebem `401` no login | usuário ou senha errados; após várias falhas há bloqueio temporário (`429`) |
| `403` em `/dados` ou `/eventos` | a conta não tem a permissão ou a fonte da rota (app usa conta `idoso`, bridge usa conta `dispositivo`) |
| App diz "tipo de conta ainda não suportado" | a conta usada não é do papel `idoso` |
| Celular não alcança o servidor | IP/porta errados, redes diferentes ou firewall bloqueando a porta 5000 |
| Bridge fica "tentando reconectar" | simulação não iniciada, ou aba do Wokwi oculta |
| Telemetria chega, mas o buzzer nunca toca | simulação pausada (aba oculta); sketch não recompilado depois da mudança; comando expirou antes de chegar (o servidor registra a ausência do ack em `confirmacoes_usuario_1.jsonl`) |
| Erro de compilação em `tone()`/`noTone()` | core `esp32:esp32` abaixo da versão 3.x; atualize com `arduino-cli core upgrade esp32:esp32` |
| App não toca o alerta | app em segundo plano ou tela bloqueada; o servidor registra `SEM_CANAL_ATIVO` |
