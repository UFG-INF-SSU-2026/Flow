# Flow — Instruções de execução (Marco 3)

Este projeto une um app Android, uma simulação Wokwi (ESP32 + acelerômetro + buzzer), uma ponte (bridge) e um servidor central. O servidor recebe telemetria das duas fontes, associa-as à **mesma pessoa** pela conta autenticada, analisa o acelerômetro e, se concluir que o idoso provavelmente caiu, **empurra (push) um pedido de confirmação** de volta aos dispositivos.

## Estrutura

```dir
Flow/
├── SensorServer/       # servidor central (Flask): borda (autenticação, banco, push) e análise (decide)
│   ├── .env.example    # modelo da configuração do servidor (copie para .env)
│   ├── borda/          # Servidor 1: rotas, auth, SSE, banco; chama a análise por HTTP local
│   ├── analise/        # Servidor 2: núcleo e regras (queda, imobilidade, luz); sem banco
│   ├── comum/          # constantes, schemas, mensagem de push, contrato borda↔análise e TLS
│   ├── certs/          # CA do projeto e certificados TLS (gerados por scripts/gerar_certificado.py; fora do Git)
│   ├── flow_db/        # banco (uma migração, interface única de acesso)
│   ├── tests/          # suíte pytest do servidor (unit/ e integration/)
│   └── simular_queda.py  # simula uma queda sem Wokwi nem celular
├── SensorApp/          # app Android (Kotlin): telemetria + recebe o pedido de confirmação
│   └── app/src/test/   # testes de unidade (JVM) da validação das mensagens de push
├── WokwiBridge/        # ponte Python bidirecional entre a simulação Wokwi e o servidor
│   ├── .env.example    # modelo da configuração do bridge (copie para .env)
│   └── tests/          # test_bridge.py (pytest) e fake_firmware.py (testa a ponte sem Wokwi)
├── sketch/             # projeto Wokwi (ESP32 + MPU6050 + buzzer), simulado no VS Code
│   └── host_test/      # teste opcional da lógica do firmware no PC
├── docs/               # arquitetura e schemas
├── atividades/         # entregas das atividades da disciplina
├── INSTRUCOES.md       # este arquivo
└── README.md           # análise e resumo do projeto
```

## Como as peças se falam

- **Dispositivo → servidor:** `POST` HTTPS (telemetria em `/dados`, ack e resposta do idoso em `/eventos`), sempre com token.
- **Servidor → dispositivo:** o **dispositivo abre** uma conexão SSE com o servidor e o servidor empurra mensagens por ela. Nenhum dispositivo escuta porta; por isso o único requisito de rede é que o celular e o bridge **alcancem o servidor**.
- **Servidor 1 ↔ Servidor 2:** a borda (porta 5000) é a única que fala com os dispositivos e com o banco; a análise só escuta em `127.0.0.1:5100` e só a borda a chama.
- **TLS em todas as pontas:** dispositivos ↔ borda, borda ↔ análise e borda ↔ banco falam HTTPS/TLS (o banco, SQL sobre TLS), com a **CA própria do projeto**: ela assina o certificado de cada serviço e é a única confiança de cada cliente. Ver a seção "Certificado TLS" abaixo.
- **Wokwi:** o ESP32 simulado só fala serial. O bridge lê e escreve nessa serial (RFC2217, `localhost:4000`) e faz a ponte com o servidor.

## Pré-requisitos

| Item | Observação |
| --- | --- |
| Python 3.10 ou superior | servidor e bridge |
| `arduino-cli` com o core `esp32:esp32` **3.x** | compila o sketch (o buzzer usa `tone()`/`noTone()`). Confira a versão com `arduino-cli core list` |
| VS Code + extensão **Wokwi** | simulação local (não pelo site) |
| Android Studio | gerar o APK, ou usar o APK das releases |
| Computador e celular na **mesma rede Wi-Fi** | o celular acessa o IP do computador |

## 0. Configuração (`.env`), segredo e contas (uma vez)

A configuração do servidor e a do bridge ficam em arquivos **`.env`**, um em cada pasta (nada de redefinir variáveis em cada terminal). Cada pasta traz um modelo, o `.env.example`; copie-o para `.env` e preencha:

```powershell
# Windows (PowerShell)
Copy-Item SensorServer\.env.example SensorServer\.env
Copy-Item WokwiBridge\.env.example WokwiBridge\.env
```

```bash
# Linux/macOS
cp SensorServer/.env.example SensorServer/.env
cp WokwiBridge/.env.example WokwiBridge/.env
```

Regras:

- Os `.env` **não vão para o Git** (só os `.env.example`, sem segredos).
- Uma variável de ambiente já definida no terminal **vence** o arquivo (útil para um teste pontual, como `FLOW_BRIDGE_DEVICE=esp32-falso`).
- Valores com espaço ou `#` (uma senha, por exemplo) vão entre aspas: `FLOW_BRIDGE_SECRET="minha#senha"`. O `$` é literal.
- Arquivo ausente não é erro: nesse caso valem só as variáveis de ambiente. Ao subir, o servidor e o bridge informam se leram o `.env`.

**Servidor.** Um só `SensorServer/.env`, com três valores obrigatórios (a borda não sobe sem os dois primeiros; o banco, sem o terceiro). Gere cada um e coloque no `.env`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"   # FLOW_JWT_SECRET (assinatura dos tokens)
python -c "import secrets; print(secrets.token_urlsafe(24))"   # FLOW_ANALISE_TOKEN (borda <-> análise); repita para FLOW_DB_PASSWORD (papel da borda) e FLOW_DB_ADMIN_PASSWORD (administrativo)
```

```ini
FLOW_JWT_SECRET=<valor gerado>
FLOW_ANALISE_TOKEN=<valor gerado>
FLOW_DB_PASSWORD=<valor gerado>          # papel flow_borda (a borda; sem DDL)
FLOW_DB_ADMIN_PASSWORD=<valor gerado>    # papel administrativo (docker compose, python -m flow_db, backup)
```

O papel `flow_borda` é criado por `python -m flow_db migrar` (o `scripts/preparar.*` o executa). As variáveis `FLOW_DB_ADMIN_*` só são lidas pelo `docker compose` e por `python -m flow_db`; a borda nunca as recebe.

A análise lê desse arquivo **só** as variáveis `FLOW_ANALISE_*` e `FLOW_TLS_*` (caminhos da CA, do certificado e da chave): o segredo dos tokens e a senha do banco nunca entram no ambiente dela. (Sem `.env`, é possível exportar as variáveis no terminal.)

Crie as duas contas do protótipo (a senha é pedida no terminal). As contas ficam no banco, então ele precisa estar no ar (`scripts/preparar.sh` o sobe) e o `.env` preenchido: Ambas apontam para a **mesma pessoa** (`--user-id 1`), e é isso que faz o servidor juntar app e wokwi:

```bash
cd SensorServer
pip install -r requirements.txt
python -m borda.auth criar-conta --id idoso1  --papel idoso       --user-id 1 --fontes app
python -m borda.auth criar-conta --id bridge1 --papel dispositivo --user-id 1 --fontes wokwi --device-id esp32-decisao-imobilidade-01
```

A conta do bridge (papel `dispositivo`) **lista os `device_id` que pode usar** (`--device-id`, repita para vários): o do firmware (o `DEVICE_ID` de `sketch.ino`, o mesmo de `FLOW_BRIDGE_DEVICE`) precisa estar na lista, senão o login leva `403` (`device_nao_permitido`). O `idoso1` (app) também informa um `device_id` no login (o app já o envia), mas ele é livre. Os dois logins são julgados **pelo servidor**: o bridge só repassa o login.

> O ID do usuário **não é mais digitado** no app nem no bridge: ele vem da conta. Para simular outra pessoa, crie outro par de contas com outro `--user-id` (uma conta e um `user_id` por simulação isolam os pedidos de confirmação: contas de `user_id` diferentes nunca recebem o push uma da outra).
>
> Para criar contas sem o prompt de senha (scripts), defina `FLOW_SENHA` no ambiente antes de rodar o comando. A senha das contas **não** fica no `.env` do servidor.

Contas e sessões ficam nas tabelas `contas` e `sessoes` do banco; não há arquivo de contas em disco. Se o volume do banco for recriado (`docker compose down -v`), as contas somem junto: crie-as de novo.

### Certificado TLS (uma vez)

Toda comunicação é cifrada com TLS. O projeto tem uma **CA própria**, que assina o certificado de cada serviço (borda, análise e banco). Gere tudo em `SensorServer/` (o `scripts/preparar.*` já faz isso para o que faltar):

```bash
cd SensorServer
python scripts/gerar_certificado.py --ip <IP do computador na rede>   # ex.: --ip 192.168.0.10
```

Isso cria, em `SensorServer/certs/` (**fora do Git**): `ca.crt` e `ca.key` (a chave da CA é segredo; só o script a lê), e `borda.crt`/`borda.key`, `analise.crt`/`analise.key` e `banco.crt`/`banco.key`. Copia ainda o `ca.crt` (público) para `SensorApp/app/src/main/res/raw/flow_cert.pem`. O certificado da borda só vale para os endereços listados nele (`localhost`, `127.0.0.1`, `::1`, o IP de rede detectado e os de `--ip`/`--dns`); quem acessa por outro endereço recusa a conexão. Para acrescentar ou trocar um IP: `python scripts/gerar_certificado.py --forcar --ip <IP>`, depois `docker compose restart db` e reinicie borda e análise. Isso **não troca a CA**: o app e o bridge continuam confiando, **sem recompilar**. Só `--forcar-ca` recria a CA (e então o app precisa ser recompilado). Para a API HTTPS do bridge, `--nome bridge` emite `certs/bridge.crt`.

Quem confia na CA (e só nela): a borda (para a análise e para o banco), o bridge (`FLOW_TLS_CERT` no `WokwiBridge/.env`, apontando para `../SensorServer/certs/ca.crt`) e o app (embutida na compilação). Se o bridge rodar em outra máquina, copie para lá só o `ca.crt`, nunca uma chave. Gere os certificados **antes** de abrir o app no Android Studio.

## 1. Servidor (`SensorServer/`)

Com o `SensorServer/.env` preenchido, suba o banco, a borda e a análise (a ordem entre borda e análise é indiferente; detalhes na seção "Dois processos", abaixo):

```bash
cd SensorServer
scripts/preparar.sh      # banco (Docker) + migração; no Windows: scripts\preparar.ps1
scripts/tudo.sh          # borda + análise (Windows: scripts\tudo.ps1)
```

Sem Docker, só para ver as rotas: `FLOW_DB_FAKE=1 python -m borda.server` (banco em memória; sem a análise rodando ninguém decide queda).

A borda escuta **só em HTTPS** e, por padrão, **só neste computador** (`127.0.0.1:5000`). Para o celular ou o bridge em outra máquina, descubra o IP do computador (`ipconfig` no Windows, `ifconfig`/`ip a` no Mac/Linux), gere o certificado com ele (`--ip`) e ponha no `SensorServer/.env` `FLOW_BORDA_HOST=<IP>` (a porta se troca com `FLOW_BORDA_PORT`). O celular vai precisar desse IP.

**Firewall:** com `FLOW_BORDA_HOST` no IP da rede, libere a **entrada** na porta TCP 5000 no computador. O celular e o bridge conectam *no servidor*; nada conecta no celular.

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

O bridge lê a configuração do `WokwiBridge/.env` (nada de editar o `bridge.py`):

| Chave | Valor | Padrão |
| --- | --- | --- |
| `FLOW_BRIDGE_ID` | `bridge1` | — (obrigatória) |
| `FLOW_BRIDGE_SECRET` | senha da conta `bridge1` | — (obrigatória) |
| `FLOW_SERVIDOR` | URL base do servidor (precisa ser `https://`) | `https://127.0.0.1:5000` |
| `FLOW_TLS_CERT` | arquivo em que o bridge confia: o `ca.crt` da CA do projeto | defina `../SensorServer/certs/ca.crt` (o padrão do código ainda é o nome do certificado antigo) |
| `FLOW_BRIDGE_DEVICE` | `deviceId` do firmware que o bridge repassa | `esp32-decisao-imobilidade-01` (o `DEVICE_ID` de `sketch.ino`) |

O servidor exige que o `deviceId` dos eventos seja o mesmo informado no login do bridge (`403` se diferir). Só é preciso definir `FLOW_BRIDGE_DEVICE` se você mudar o `DEVICE_ID` do firmware.

> No Windows, prefira `127.0.0.1` a `localhost` em `FLOW_SERVIDOR`: o `localhost` tenta IPv6 primeiro e cada conexão nova do bridge espera 1-2 s, o que atrasa a telemetria e as respostas ao servidor (o pedido de confirmação pode expirar antes de a resposta chegar).

Exemplo de `WokwiBridge/.env`:

```ini
FLOW_BRIDGE_ID=bridge1
FLOW_BRIDGE_SECRET=<senha da conta bridge1>
FLOW_SERVIDOR=https://127.0.0.1:5000
```

Depois é só rodar:

```bash
python bridge.py
```

(Sem `.env`, as mesmas chaves podem ser definidas no terminal, como antes: `export FLOW_BRIDGE_ID=bridge1` no Linux/macOS, ou `$env:FLOW_BRIDGE_ID='bridge1'` no PowerShell, com aspas simples.)

O bridge faz login, abre o canal de push do servidor, repassa a telemetria da simulação e escreve na serial os comandos do buzzer. Se a simulação ainda não estiver de pé, ele fica tentando reconectar.

## 4. App Android (`SensorApp/`)

Gere a CA e os certificados antes (seção "Certificado TLS"): o app embute `res/raw/flow_cert.pem` (o `ca.crt`) e só fala HTTPS com um servidor cujo certificado tenha sido emitido por essa CA. Abra no Android Studio e gere o APK (o das releases só serve se a CA for a mesma). Depois de instalado:

1. Toque em **Configurações**.
2. Preencha **IP** (o mesmo que foi para o certificado) e **porta** do servidor, e o **usuário** (`idoso1`) e a **senha** da conta criada no passo 0. Salve.
3. Toque em **Iniciar** e **mantenha o app aberto e em primeiro plano**.

Observações importantes:

- O app só envia dados **e só recebe o pedido de confirmação** enquanto está aberto e em primeiro plano. Com a tela bloqueada ou o app em segundo plano, ele não é alertado (limitação conhecida, ver `docs/arquitetura.md`, seção 13.2).
- Este app aceita apenas contas do tipo `idoso`. Outros tipos de conta serão atendidos no futuro pelo mesmo login.

## Testes automatizados

### Servidor (pytest)

```bash
cd SensorServer
pip install -r requirements.txt   # inclui o pytest
python -m pytest
```

A configuração está em `SensorServer/pytest.ini`. A suíte tem `tests/unit/` (módulos isolados), `tests/integration/` (rotas HTTP, push SSE, autorização, limite de corpo, banco e análise fora do ar e ciclo de queda ponta a ponta) e `tests/banco/` (o mesmo contrato do repositório no PostgreSQL real; fora da execução padrão: `python -m pytest -m banco`, com o container no ar). Nos testes a análise roda em memória no lugar do processo. Ela é independente do seu ambiente: **não lê o seu `.env`** (`FLOW_SEM_DOTENV=1`), define o próprio `FLOW_JWT_SECRET`, desliga as threads de fundo (`FLOW_SEM_THREADS=1`), usa um banco falso em memória (que também guarda contas e sessões), então **não usa nem altera** as suas contas reais.

### Bridge (pytest)

```bash
cd WokwiBridge
pip install -r requirements.txt   # inclui o pytest
python -m pytest
```

A configuração está em `WokwiBridge/pytest.ini`. Os testes não usam rede nem serial, e não leem o seu `.env`: cobrem a validação e a tradução das mensagens de push, o roteamento das linhas que vêm do firmware, o leitor SSE e a renovação de sessão. O `tests/fake_firmware.py` (abaixo) não é coletado pelo pytest.

### App Android (JVM)

Os testes de `SensorApp/app/src/test/` (validação da forma das mensagens de push) rodam na JVM, sem emulador. No Android Studio, clique com o botão direito em `app/src/test` e escolha **Run Tests**; ou rode `gradle test` dentro de `SensorApp`. A primeira execução baixa o JUnit e o `org.json`.

### Fora do pytest (opcionais)

| O quê | Como | Para que serve |
| --- | --- | --- |
| `SensorServer/simular_queda.py` | com o servidor rodando: `FLOW_BRIDGE_ID=bridge1 FLOW_BRIDGE_SECRET=<senha> python simular_queda.py [URL_BASE]` (URL padrão `https://127.0.0.1:5000`) (ainda lê essas duas do terminal; não usa o `.env` do bridge) | Simula um wokwi que sofre impacto e fica imóvel; o pedido de confirmação chega ao app e/ou ao bridge conectados, sem Wokwi. |
| `WokwiBridge/tests/fake_firmware.py` | na pasta `WokwiBridge`, com servidor e conta do bridge, o `WokwiBridge/.env` preenchido e `FLOW_BRIDGE_DEVICE=esp32-falso` (variável do terminal, que vence o `.env`): `python tests/fake_firmware.py` | Substitui a serial por um "firmware falso" e confere o que o bridge escreve. O `FLOW_BRIDGE_DEVICE` precisa ser o `deviceId` que o firmware falso usa (`esp32-falso`). |
| `sketch/host_test/` | ver `sketch/host_test/README.md` | Roda a lógica do firmware no PC (inclui o buzzer e a idempotência do `BUZZER_ON`). |

O servidor mantém um **cooldown de 60 s** entre episódios do mesmo usuário (`COOLDOWN_S`): para repetir `simular_queda.py` ou `fake_firmware.py` em seguida, reinicie o servidor ou aguarde.

## Testando manualmente

### Autenticação

Sem token, o servidor recusa:

```bash
curl -i --cacert SensorServer/certs/ca.crt -X POST https://localhost:5000/dados -H "Content-Type: application/json" -d "{}"
```

Esperado: `401`. Tentativas negadas (`401`, `403` e `413`) aparecem no log da borda (`[authz] negado ...`), sem o conteúdo da requisição.

### Leituras no banco

Com servidor, app e bridge rodando, confira:

```bash
docker compose exec -T db psql -U flow -d flow -c "SELECT instante_ingestao, user_id, fonte FROM leituras ORDER BY id DESC LIMIT 5"
```

Cada leitura aceita é uma linha, de `app` ou de `wokwi`, ligada ao usuário da conta. E `curl --cacert SensorServer/certs/ca.crt https://localhost:5000/health` mostra se a análise está respondendo (`analise.estado`: `ok` ou `degradado`).

### Queda e confirmação — Wokwi

1. Com a simulação rodando, leve os sliders do acelerômetro a um valor alto (impacto) por um instante.
2. Zere todos os sliders e mantenha parado por alguns segundos.
3. O servidor deve concluir "queda provável" (impacto seguido de imobilidade) e o **buzzer toca**. Pressione **Estou bem** em até 5 s: o buzzer cala.
4. Se não pressionar, o buzzer cala ao fim do prazo.

Sem impacto algum, deixar tudo zerado por tempo suficiente também dispara o pedido, por **imobilidade prolongada**, mas com mais atraso (o gatilho de impacto tem preferência). O campo `motivo` do registro `episodio.aberto` (tabela `confirmacoes`) mostra qual gatilho abriu o episódio.

### Queda e confirmação — App

Com o app em primeiro plano e **Iniciar** ativo, simule um impacto (movimento brusco no aparelho) e deixe-o imóvel. O app toca o alerta e mostra o botão **Estou bem**, que some após 5 s.

### O que o servidor guarda

```bash
docker compose exec -T db psql -U flow -d flow -c "SELECT instante, tipo, dados->>'resultado' AS resultado FROM confirmacoes WHERE user_id = 1 ORDER BY id"
```

Cada episódio deixa linhas de abertura, ack dos dispositivos, resposta do idoso (`ESTOU_BEM` ou `SEM_RESPOSTA`) e encerramento; as mensagens enviadas ficam em `comandos_push` e os alertas em `alertas`. **O servidor apenas armazena a resposta**; o que fazer com ela ainda não foi definido.

Se app e wokwi estiverem ligados à mesma pessoa, o pedido toca nos dois ao mesmo tempo, e o primeiro "Estou bem" silencia o outro.

## Ajustes comuns

| O que ajustar | Onde |
| --- | --- |
| Tempo do botão "Estou bem" (padrão 5 s) | `TIMEOUT_CONFIRMACAO_S` em `SensorServer/comum/const.py` (vale para app e wokwi, sem recompilar nada) |
| Limiares de impacto e de repouso | `LIMIAR_IMPACTO` e `LIMIAR_REPOUSO` em `SensorServer/analise/const.py` (valores iniciais, a calibrar) |
| Validade dos tokens | `ACCESS_TTL_S` e `REFRESH_TTL_S` em `SensorServer/borda/const.py` |
| Tamanho máximo do corpo de uma requisição (padrão 64 KiB) | `MAX_CONTENT_LENGTH` em `SensorServer/borda/const.py` |
| Intervalo mínimo entre episódios do mesmo usuário (padrão 60 s) | `COOLDOWN_S` em `SensorServer/analise/const.py` |
| Intervalo de envio do app | `SEND_INTERVAL_MS` em `MainActivity.kt` (padrão 1 s) |

## Problemas comuns

| Sintoma | Causa provável |
| --- | --- |
| Servidor não sobe e reclama de segredo | `FLOW_JWT_SECRET` ausente (ou com menos de 32 bytes) em `SensorServer/.env`, e não definido no terminal em que o servidor foi iniciado. Ao subir, o servidor mostra se leu o `.env` |
| App ou bridge recebem `401` no login | usuário ou senha errados (no bridge: `FLOW_BRIDGE_SECRET` do `.env`, ou uma variável do terminal que sobrepõe o arquivo); após várias falhas há bloqueio temporário (`429`) |
| Login do bridge recusado com `403` (`device_nao_permitido`) ou `400` (`device_id_obrigatorio`) | o `device_id` do firmware (`FLOW_BRIDGE_DEVICE`) não está na lista da conta `bridge1`, ou não foi enviado: recrie a conta com `--device-id <o do firmware>` (ou o `down -v` e as contas de novo). Não conta como falha de senha |
| Bridge lento: eventos atrasados, pedido de confirmação `EXPIRADO` antes do ack | `FLOW_SERVIDOR` usando `localhost` no Windows (IPv6 primeiro); use `https://127.0.0.1:5000` |
| Borda ou análise não sobem: "certificado TLS nao encontrado" (ou "certificado da CA") | falta o par do serviço (`certs/borda.*` ou `certs/analise.*`) ou o `certs/ca.crt`; rode `python scripts/gerar_certificado.py` em `SensorServer/` |
| Bridge, app ou borda recusam a conexão (`certificate verify failed`, `SSLError`, "falha de conexão" no app) | o endereço usado não está no certificado da borda (reemita com `--forcar --ip <IP>`; a CA não muda, então ninguém recompila), ou o cliente confia em outra CA (copie o `ca.crt` atual para o bridge; se a CA foi recriada com `--forcar-ca`, recompile o app) |
| Container do banco não sobe, ou a borda não conecta depois de reemitir o certificado | faltam `certs/banco.crt`/`banco.key` na hora do `docker compose up`, ou o container ainda tem o par antigo: `docker compose restart db` |
| Compilação do app falha por `@raw/flow_cert` | a CA ainda não foi gerada: rode `python scripts/gerar_certificado.py` antes (ele copia o `ca.crt` para `SensorApp/app/src/main/res/raw/`) |
| `403` em `/dados` ou `/eventos` | a conta não tem a permissão ou a fonte da rota (app usa conta `idoso`, bridge usa conta `dispositivo`); ou o `deviceId` dos eventos difere do `FLOW_BRIDGE_DEVICE` usado no login do bridge |
| `413` | corpo da requisição acima de `MAX_CONTENT_LENGTH` (64 KiB); o servidor não processa o conteúdo |
| Pedido de confirmação não abre de novo logo após um teste | cooldown de 60 s entre episódios; reinicie o servidor ou aguarde |
| App diz "tipo de conta ainda não suportado" | a conta usada não é do papel `idoso` |
| Celular não alcança o servidor | IP/porta errados, redes diferentes ou firewall bloqueando a porta 5000 |
| Bridge fica "tentando reconectar" | simulação não iniciada, ou aba do Wokwi oculta |
| Telemetria chega, mas o buzzer nunca toca | simulação pausada (aba oculta); sketch não recompilado depois da mudança; comando expirou antes de chegar (o servidor registra a ausência do ack na tabela `confirmacoes`) |
| Erro de compilação em `tone()`/`noTone()` | core `esp32:esp32` abaixo da versão 3.x; atualize com `arduino-cli core upgrade esp32:esp32` |
| App não toca o alerta | app em segundo plano ou tela bloqueada; o log da borda mostra `[push] ... SEM_CANAL_ATIVO` |

## Como as decisões chegam ao app e ao dispositivo

A borda grava a leitura e a manda à **análise** (`POST /analisar`); a análise responde com decisões e a borda as grava numa transação (`comandos_push`, `alertas`, `confirmacoes`, `episodios`, `estado_dispositivo`) e só então entrega o push por SSE ao app e ao wokwi. Uma thread da borda chama a análise a cada 0,25 s (`POST /vigiar`) para encerrar como `EXPIRADO` os episódios que ninguém respondeu. Decisão sobre item velho (mais que `IDADE_MAX_ACAO_S`) só reconstrói estado e não age. `/estado/<device_id>` lê o resumo gravado em `estado_dispositivo`, então reflete a última decisão (não a leitura que acabou de chegar).

Sem a análise rodando, a borda continua aceitando e gravando dados, mas nada é decidido: `/health` mostra `analise.estado = degradado`. Quando ela volta (ou reinicia), a borda detecta o `boot_id` novo, manda o estado de volta a partir do banco e reenvia a fila.

## Dois processos: borda e análise

| Processo | Comando (em `SensorServer/`) | Lê do `.env` | Porta |
| --- | --- | --- | --- |
| Borda (Servidor 1) | `python -m borda` | tudo | 5000 (a única exposta) |
| Análise (Servidor 2) | `python -m analise` | só `FLOW_ANALISE_*` | 5100, só em `127.0.0.1` |

O banco é só da borda. `python -m borda.server` continua importável (Flask/WSGI, testes e desenvolvimento), mas é só a borda: o ponto de entrada oficial é `python -m borda`.

**1. `.env` (uma vez).** `cp SensorServer/.env.example SensorServer/.env` (PowerShell: `Copy-Item`) e preencha `FLOW_JWT_SECRET`, `FLOW_ANALISE_TOKEN`, `FLOW_DB_PASSWORD` e `FLOW_DB_ADMIN_PASSWORD` (seção 0). **Banco antigo:** o banco agora usa a imagem do TimescaleDB (`leituras` é uma hypertable); se você já tinha um volume criado antes, com a imagem `postgres:17` simples, rode `docker compose down -v` (apaga os dados de teste) e suba de novo.

**2. Subir.** Os scripts de `SensorServer/scripts/` fazem tudo na ordem certa (a ordem entre borda e análise é indiferente):

| Ação | Linux/macOS | Windows (PowerShell) |
| --- | --- | --- |
| Banco + migração | `scripts/preparar.sh` | `scripts\preparar.ps1` |
| Só a borda | `scripts/borda.sh` | `scripts\borda.ps1` |
| Só a análise | `scripts/analise.sh` | `scripts\analise.ps1` |
| Tudo | `scripts/tudo.sh` | `scripts\tudo.ps1` (uma janela por serviço) |

Contas ficam no banco: `python -m borda.auth criar-conta` (com o banco no ar; sem ele o comando recusa com uma mensagem clara).

**3. Desligar.** Ctrl+C (ou SIGTERM; Ctrl+Break no Windows) em cada processo. A borda para de aceitar, para as threads de fundo, fecha os streams SSE (os clientes reconectam), descarrega a fila de leituras e fecha o pool. Reiniciar a análise não desconecta ninguém: a borda restaura o estado dela do banco.

**4. O que ficou para depois.** Registrado em `docs/arquitetura.md` (seção 15.1): o serviço de notificação aos cuidadores e a chave AES por dispositivo.

**Observações de operação.**

- `FLOW_DB_FAKE=1` (banco em memória) é só para desenvolvimento: os dados somem ao parar a borda.
- Se a análise ficar fora, as leituras continuam sendo gravadas; ao voltar, ela recebe a fila (dentro de `IDADE_MAX_ACAO_S`; o que for mais velho só reconstrói estado).
