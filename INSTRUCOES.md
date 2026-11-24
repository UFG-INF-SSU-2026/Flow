# Flow — Instruções de execução (Marco 3)

Este projeto une um app Android, uma simulação Wokwi (ESP32 + acelerômetro + buzzer), uma ponte (bridge) e um servidor central. O servidor recebe telemetria das duas fontes, associa-as à **mesma pessoa** pela conta autenticada, analisa o acelerômetro e, se concluir que o idoso provavelmente caiu, **empurra (push) um pedido de confirmação** de volta aos dispositivos.

## Estrutura

```dir
Flow/
├── SensorServer/       # servidor central (Flask): borda (autenticação, banco, push) e análise (decide)
│   ├── .env.example    # modelo da configuração do servidor (copie para .env)
│   ├── borda/          # Servidor 1: rotas, auth, SSE, banco; chama a análise por HTTPS local
│   ├── analise/        # Servidor 2: núcleo e regras (queda, imobilidade, luz); sem banco
│   ├── comum/          # constantes, schemas, mensagem de push, contrato borda↔análise e TLS
│   ├── certs/          # CA do projeto e certificados TLS (gerados por scripts/gerar_certificado.py; fora do Git)
│   ├── flow_db/        # banco (uma migração, interface única de acesso)
│   ├── tests/          # suíte pytest do servidor (unit/ e integration/)
│   └── simular_queda.py  # simula uma queda sem Wokwi nem celular
├── SensorApp/          # app Android (Kotlin): telemetria + recebe o pedido de confirmação
│   └── app/src/test/   # testes de unidade (JVM) da validação das mensagens de push
├── Bridge/             # ponte Python (API HTTPS) entre a simulação Wokwi e o servidor
│   ├── .env.example    # modelo da configuração do bridge (copie para .env)
│   └── tests/          # suíte pytest do bridge
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
<<<<<<< Updated upstream
- **Certificado de cliente (app e wokwi):** cada dispositivo **começa sem certificado**. Ele faz login, gera no próprio aparelho um par de chaves EC P-256 e envia só o pedido (CSR) a `POST /auth/certificado`; o servidor devolve um certificado de cliente assinado pela **CA de dispositivos**, com a conta e o `device_id` **do token** e validade de 7 dias. Daí em diante, ele abre uma conexão nova apresentando esse certificado e o usa em todas as chamadas. Login, refresh e a própria emissão passam sem ele; `/dados`, `/eventos` e o push (`/push/app`, `/push/wokwi`) o exigem, e a borda confere se a conta e o `device_id` dele são os do token. O **app** apresenta o certificado direto à borda. O **wokwi** o apresenta ao bridge, que termina o TLS do ESP32 e, ao chamar a borda, se identifica com o certificado do próprio bridge e repassa o do dispositivo (cabeçalho `X-Flow-Certificado-Cliente`); a borda só aceita esse repasse de uma conexão do bridge e confere o certificado de novo. Nenhum certificado ou chave de cliente vai dentro do APK nem do firmware.
- **Wokwi → bridge:** o firmware do ESP32 simulado fala **HTTPS direto** com a API do bridge (`https://host.wokwi.internal:5443`), pelo WiFi simulado. O nome `host.wokwi.internal` é o seu PC visto de dentro da simulação, pelo gateway de rede do Wokwi (`wokwigw`). Não há serial, nem porta RFC2217, nem adaptador no caminho.
=======
- **Wokwi → bridge:** o firmware do ESP32 simulado fala **HTTPS direto** com a API do bridge (`https://host.wokwi.internal:5443`), pelo WiFi simulado. O nome `host.wokwi.internal` é o seu PC visto de dentro da simulação, pelo gateway de rede do Wokwi (`wokwigw`). Não há serial, nem porta RFC2217, nem adaptador no caminho. O firmware é o **dono do protocolo**: faz o próprio login, cifra as mensagens (AES-256-GCM), gera a chave e pede um **certificado de cliente** (mTLS com o bridge) e recebe o push por SSE; o bridge só repassa (`/auth/login`, `/auth/certificado`, `/auth/refresh`, `/dados`, `/eventos`, `GET /push/wokwi`) e repassa o certificado do dispositivo à borda (quem exige o certificado, por rota, é a borda).
>>>>>>> Stashed changes

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
Copy-Item Bridge\.env.example Bridge\.env
```

```bash
# Linux/macOS
cp SensorServer/.env.example SensorServer/.env
cp Bridge/.env.example Bridge/.env
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

Backup e restauração: `scripts/backup.sh [--testar]` (Windows: `scripts\backup.ps1 [-Testar]`), detalhes em `docs/banco.md`. O papel `flow_borda` é criado por `python -m flow_db migrar` (o `scripts/preparar.*` o executa). As variáveis `FLOW_DB_ADMIN_*` só são lidas pelo `docker compose` e por `python -m flow_db`; a borda nunca as recebe.

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

O identificador da conta (`--id`) tem **no máximo 64 caracteres**: ele vai, com o `device_id` do login, no certificado de cliente do dispositivo. Use **sempre o mesmo `device_id`** num mesmo aparelho: um certificado emitido para um `device_id` (ou uma conta) não vale para outro (`403 certificado_incompativel`).

Contas e sessões ficam nas tabelas `contas` e `sessoes` do banco; não há arquivo de contas em disco. Se o volume do banco for recriado (`docker compose down -v`), as contas somem junto: crie-as de novo.

### Certificado TLS (uma vez)

Toda comunicação é cifrada com TLS. O projeto tem uma **CA própria**, que assina o certificado de cada serviço (borda, análise e banco). Gere tudo em `SensorServer/` (o `scripts/preparar.*` já faz isso para o que faltar):

```bash
cd SensorServer
python scripts/gerar_certificado.py --ip <IP do computador na rede>   # ex.: --ip 192.168.0.10
```

<<<<<<< Updated upstream
Isso cria, em `SensorServer/certs/` (**fora do Git**): `ca.crt` e `ca.key` (a chave da CA é segredo; só o script a lê), e `borda.crt`/`borda.key`, `analise.crt`/`analise.key` e `banco.crt`/`banco.key`. Cria também a **CA de dispositivos**, `ca-dispositivos.crt` e `ca-dispositivos.key`: é uma segunda CA, independente da raiz, que só assina os certificados de cliente do app e do wokwi (a chave é segredo e **só a borda a lê**). A **borda não sobe sem `ca-dispositivos.crt`**; sem a `.key`, só a emissão de certificados fica indisponível (`503`) e os certificados já emitidos continuam valendo. O script cria a CA de dispositivos quando ela falta, em qualquer execução, e nunca a recria (nem com `--forcar` nem com `--forcar-ca`); para trocá-la, apague os dois arquivos e rode o script de novo. Os caminhos podem ser trocados por `FLOW_CA_DISPOSITIVOS_CERT` e `FLOW_CA_DISPOSITIVOS_KEY` no `SensorServer/.env` (os padrões servem). Copia ainda o `ca.crt` (público) para `SensorApp/app/src/main/res/raw/flow_cert.pem`. O certificado da borda só vale para os endereços listados nele (`localhost`, `127.0.0.1`, `::1`, o IP de rede detectado e os de `--ip`/`--dns`); quem acessa por outro endereço recusa a conexão. Para acrescentar ou trocar um IP: `python scripts/gerar_certificado.py --forcar --ip <IP>`, depois `docker compose restart db` e reinicie borda e análise. Isso **não troca a CA**: o app e o bridge continuam confiando, **sem recompilar**. Só `--forcar-ca` recria a CA (e então o app precisa ser recompilado). Para o bridge, **emita também o certificado dele com o nome que o firmware usa**: `python scripts/gerar_certificado.py --nome bridge --dns host.wokwi.internal` (o certificado do bridge precisa listar `host.wokwi.internal`, senão o firmware recusa a conexão). O mesmo `bridge.crt` serve a API HTTPS do bridge e o **identifica como cliente** quando ele chama a borda: só o certificado de nome `bridge` sai com esse uso (`clientAuth`). Um `bridge.crt` emitido antes disso não tem o uso e a borda o recusa no handshake: reemita com `--forcar --nome bridge --dns host.wokwi.internal` (a CA não muda).

Quem confia na CA (e só nela): a borda (para a análise e para o banco), o bridge (`FLOW_TLS_CA` no `Bridge/.env`, apontando para `../SensorServer/certs/ca.crt`) e o firmware (`FLOW_CA_PEM` em `sketch/segredos.h`, só o conteúdo de `ca.crt`) e o app (embutida na compilação). Para os **certificados de cliente**, a borda confia na CA de dispositivos (app e dispositivos) e na raiz (só para o certificado do bridge). Se o bridge rodar em outra máquina, copie para lá o `ca.crt` e o par do bridge (`bridge.crt`/`bridge.key`); nunca `ca.key`, `ca-dispositivos.key` nem a chave de outro serviço. Gere os certificados **antes** de abrir o app no Android Studio.
=======
Isso cria, em `SensorServer/certs/` (**fora do Git**): `ca.crt` e `ca.key` (a chave da CA é segredo; só o script a lê), `ca-dispositivos.crt` e `ca-dispositivos.key` (a CA que assina os certificados de cliente dos dispositivos; **só a borda** lê a chave; nunca é recriada por `--forcar` nem por `--forcar-ca`), e `borda.crt`/`borda.key`, `analise.crt`/`analise.key` e `banco.crt`/`banco.key`. Copia ainda o `ca.crt` (público) para `SensorApp/app/src/main/res/raw/flow_cert.pem` e gera `sketch/ca_flow.h` (o mesmo certificado público, para o firmware). O certificado da borda só vale para os endereços listados nele (`localhost`, `127.0.0.1`, `::1`, o IP de rede detectado e os de `--ip`/`--dns`); quem acessa por outro endereço recusa a conexão. Para acrescentar ou trocar um IP: `python scripts/gerar_certificado.py --forcar --ip <IP>`, depois `docker compose restart db` e reinicie borda e análise. Isso **não troca a CA**: o app e o bridge continuam confiando, **sem recompilar**. Só `--forcar-ca` recria a CA (e então o app precisa ser recompilado). Para a API HTTPS do bridge, **emita também o certificado dele com o nome que o firmware usa**: `python scripts/gerar_certificado.py --nome bridge --dns host.wokwi.internal` (o certificado do bridge precisa listar `host.wokwi.internal`, senão o firmware recusa a conexão).

Quem confia na CA (e só nela): a borda (para a análise e para o banco), o bridge (`FLOW_TLS_CA` no `Bridge/.env`, apontando para `../SensorServer/certs/ca.crt`), o firmware (`sketch/ca_flow.h`, **gerado** pelo script com só o conteúdo de `ca.crt`; não se cola mais a CA à mão) e o app (embutida na compilação). A CA de **dispositivos** (`ca-dispositivos.crt`/`.key`, também criada pelo script, independente da raiz) assina só os certificados de cliente dos dispositivos: o bridge lê o `.crt` (`FLOW_CA_DISPOSITIVOS_CERT`) e só a borda lê a chave. Se o bridge rodar em outra máquina, copie para lá só o `ca.crt`, nunca uma chave. Gere os certificados **antes** de abrir o app no Android Studio.
>>>>>>> Stashed changes

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

O firmware fala **somente HTTPS** com o bridge. Antes de compilar:

1. **Certificado do bridge com o nome do Wokwi** (em `SensorServer/`; os certificados de serviço são reemitidos, a CA e a CA de dispositivos continuam as mesmas):

   ```bash
   python scripts/gerar_certificado.py --forcar --nome bridge --dns host.wokwi.internal
   ```

   O `bridge.crt` agora também é a **identidade do bridge na borda** (precisa de `serverAuth` e `clientAuth` e `CN=bridge`): um emitido antes dessa mudança é recusado no handshake da borda, e o bridge **nem sobe** com ele (a mensagem diz o remédio: este mesmo comando). A borda também não sobe sem `ca-dispositivos.crt` (o script a cria quando falta).

2. **`sketch/segredos.h`:** copie `segredos.h.example` para `segredos.h` e preencha a conta (`FLOW_IDENTIFICADOR`, `FLOW_SEGREDO`) e a `FLOW_CHAVE_AES_B64` (o valor de `SHARED_KEY_B64` em `SensorServer/borda/crypto_utils.py`). A CA do firmware **não** vai neste arquivo: o passo 1 já gerou `sketch/ca_flow.h` (só o certificado público; não vai para o Git). `FLOW_BRIDGE_URL` precisa ser `https://...`: com `http://` o sketch **não compila**.

3. **Gateway de rede do Wokwi.** O ESP32 simulado só alcança o seu PC por ele. Baixe o `wokwigw` (releases de <https://github.com/wokwi/wokwigw>, código aberto) e deixe rodando num terminal **antes** de iniciar a simulação; ele escuta em `localhost:9011`, e o `sketch/wokwi.toml` já aponta para lá (`[net] gateway = "ws://localhost:9011"`). Sem ele, a simulação não alcança o PC.

4. Abra a pasta `sketch/` no VS Code com a extensão Wokwi instalada, instale a biblioteca **ArduinoJson 7** (uma vez: `arduino-cli lib install "ArduinoJson@7.4.2"`) e compile (dentro de `sketch/`):

   ```bash
   arduino-cli compile --fqbn esp32:esp32:esp32 --output-dir build .
   Caso ocorra erro relacionado ao arduino, rode o comando: arduino-cli lib install "ArduinoJson"   
   ```

    OBS: Para conseguir rodar wokwigw foi necessario fazer o download do https://github.com/wokwi/wokwigw/releasese executar em um novo terminal \wokwigw.exe
 5. Inicie a simulação (`F1` → `Wokwi: Start Simulator`) e **deixe a aba do simulador visível**: com a aba oculta a simulação pausa. No monitor serial aparecem só mensagens de diagnóstico (`WiFi conectado`, `login aceito`, `certificado emitido`, ...); nenhum evento, push, token, chave nem a senha passa pela serial. Ordem do boot (uma operação bloqueante por volta do `loop()`): login → chave e CSR → emissão do certificado → online (telemetria por mTLS; push por SSE). Enquanto o certificado não existe **nada** é enviado às rotas protegidas.
        

Se aparecer `login nao aceito (HTTP -1)`, a conexão HTTPS não se completou. Confira, nesta ordem: `wokwigw` rodando; bridge no ar; o certificado do bridge lista `host.wokwi.internal`; o `sketch/ca_flow.h` é da CA atual (recompile o firmware depois de `--forcar-ca`); e o aviso `sem horario (NTP)` (sem o horário certo o certificado não valida e nada é enviado). Mensagens do certificado: `borda: certificado ausente; emitindo outro` (`401 certificado_ausente`: a chamada chegou à borda sem certificado) e `borda: certificado de outra conta ou device; emitindo outro` (`403 certificado_incompativel`) fazem o firmware pedir outro certificado **com o mesmo token** (sem novo login); `handshake com certificado falhou 3 vezes` indica que o bridge derrubou a conexão (certificado vencido, de outra CA ou `ca-dispositivos.*` recriada). Os pedidos de certificado esperam cada vez mais (5 s até 60 s); `emissao: CSR recusado pelo servidor` (`400 csr_invalido`) é erro de programação e não repete em laço. Se persistir, confira `FLOW_CA_DISPOSITIVOS_CERT` do bridge, o `CN` e os usos do `bridge.crt` e o nome da URL no certificado do bridge (um nome fora da lista também derruba a conexão, e emitir outro certificado não resolve). Se o bridge escutar só em `127.0.0.1` e o gateway não alcançar esse endereço, ponha `FLOW_BRIDGE_HOST=0.0.0.0` no `Bridge/.env` (continua só HTTPS) e inclua o IP do PC no certificado do bridge (`--ip`).

O diagrama tem um **buzzer** (pino D13) além do acelerômetro, dos botões "Estou bem" e "Rearme" e dos LEDs. Qualquer alteração em `sketch.ino`, `transporte_wifi.h` ou `diagram.json` exige repetir a compilação e reiniciar a simulação.

## 3. Bridge (`Bridge/`)

```bash
cd Bridge
pip install -r requirements.txt
cp .env.example .env      # todas as chaves têm padrão; só mude o que precisar
python bridge.py
```

O bridge **não tem conta, senha nem token**: o login de cada dispositivo vem do próprio firmware (`sketch/segredos.h`) e o bridge só repassa as chamadas. Ele lê o `Bridge/.env` (modelo: `.env.example`):

| Chave | Valor | Padrão |
| --- | --- | --- |
| `FLOW_SERVIDOR` | URL base do servidor (precisa ser `https://`; outra coisa encerra o bridge) | `https://127.0.0.1:5000` |
| `FLOW_TLS_CA` | `ca.crt` da CA do projeto: a única confiança do bridge para falar com o servidor | `../SensorServer/certs/ca.crt` |
| `FLOW_CA_DISPOSITIVOS_CERT` | certificado **público** da CA de dispositivos: com ele o bridge confere o certificado de cliente do firmware (o bridge **não sobe** sem este arquivo; a chave dessa CA nunca passa por aqui) | `../SensorServer/certs/ca-dispositivos.crt` |
| `FLOW_BRIDGE_HOST`, `FLOW_BRIDGE_PORT` | onde a API do bridge escuta (só HTTPS) | `127.0.0.1`, `5443` |
| `FLOW_BRIDGE_API_CERT`, `FLOW_BRIDGE_API_KEY` | certificado e chave da API do bridge, que também o identificam na borda como cliente (`serverAuth` + `clientAuth`, `CN=bridge`; `gerar_certificado.py --nome bridge`) | `../SensorServer/certs/bridge.crt`, `bridge.key` |

A API do bridge é servida pelo `cheroot` (o `pip install -r requirements.txt` já o instala), sempre sobre HTTPS. A conexão TLS é reaproveitada entre as chamadas: uma conexão ociosa por mais de 10 s é fechada e a chamada seguinte paga um handshake de novo. Cada requisição gera uma linha no terminal, `[bridge] HH:MM:SS.mmm METODO /caminho STATUS`, sem query string, corpo nem cabeçalhos. `Ctrl+C` encerra o bridge.

O servidor exige que o `deviceId` dos eventos seja um dos `device_id` da conta (`403` se diferir), e o firmware usa `esp32-decisao-imobilidade-01` (o `DEVICE_ID` de `sketch.ino`).

> No Windows, prefira `127.0.0.1` a `localhost` em `FLOW_SERVIDOR`: o `localhost` tenta IPv6 primeiro e cada conexão nova do bridge espera 1-2 s.

O bridge só repassa as chamadas do firmware à borda, uma a uma, e entrega o push como fluxo contínuo; qualquer rota fora da lista (`/auth/login`, `/auth/refresh`, `/auth/certificado`, `/dados`, `/eventos`, `GET /push/wokwi`) é `404` e nem chega à borda. **Quem exige o certificado do dispositivo é a borda**: login, refresh e emissão passam sem ele; `/dados`, `/eventos` e o push respondem `401 certificado_ausente` (sem certificado) ou `403 certificado_incompativel` (certificado de outra conta ou de outro `device_id`), e o bridge repassa essas respostas como vieram. O TLS do dispositivo termina no bridge: ele pede o certificado de cliente de forma opcional, confere só contra a CA de dispositivos (outra CA, a raiz, vencido ou só `serverAuth` derrubam a conexão) e, na chamada à borda, **apaga sempre** o cabeçalho `X-Flow-Certificado-Cliente` que veio do dispositivo e acrescenta o verdadeiro (DER em base64). Para a borda saber que pode confiar nesse cabeçalho, o bridge se identifica em **toda** chamada (inclusive o SSE) com o próprio `bridge.crt`, que precisa ter `serverAuth` **e** `clientAuth` e `CN=bridge`. Limitações assumidas: a borda confia no bridge para provar que o dispositivo tem a chave (quem tiver `bridge.key` e o certificado público de um dispositivo se passa por ele, ainda precisando do token dele; `bridge.key` é segredo como as demais chaves), o bridge ainda vê o `Authorization` em trânsito (não o guarda) e quem fala direto com a borda sem certificado leva `401`. Se o firmware subir antes do bridge (ou do servidor), ele tenta o login de novo a cada 5 s; se o bridge reiniciar no meio, nada muda para o firmware (a sessão e o certificado são dele).

> **Certificado de cliente no caminho do Wokwi.** A borda exige, em `/dados`, `/eventos` e `/push/wokwi`, o certificado de cliente do dispositivo, repassado pelo bridge numa conexão em que o bridge apresenta o próprio `bridge.crt` (seção "Como as peças se falam"). Este bridge e este firmware ainda não fazem isso: o login passa, mas a telemetria, os eventos e o canal de push levam `401` (`certificado_ausente`) e nada chega ao servidor.

## 4. App Android (`SensorApp/`)

Gere a CA e os certificados antes (seção "Certificado TLS"): o app embute `res/raw/flow_cert.pem` (o `ca.crt`) e só fala HTTPS com um servidor cujo certificado tenha sido emitido por essa CA. Abra no Android Studio e gere o APK (o das releases só serve se a CA for a mesma). Depois de instalado:

1. Toque em **Configurações**.
2. Preencha **IP** (o mesmo que foi para o certificado) e **porta** do servidor, e o **usuário** (`idoso1`) e a **senha** da conta criada no passo 0. Salve.
3. Toque em **Iniciar** e **mantenha o app aberto e em primeiro plano**.

Observações importantes:

- O app só envia dados **e só recebe o pedido de confirmação** enquanto está aberto e em primeiro plano. Com a tela bloqueada ou o app em segundo plano, ele não é alertado (limitação conhecida, ver `docs/arquitetura.md`, seção 13.2).
- Este app aceita apenas contas do tipo `idoso`. Outros tipos de conta serão atendidos no futuro pelo mesmo login.
- A borda exige do app o certificado de cliente emitido por `POST /auth/certificado` (seção "Como as peças se falam"). Este app ainda não o pede: o login passa, mas o envio de dados, os eventos e o canal de push levam `401` (`certificado_ausente`).

## Testes automatizados

### Servidor (pytest)

```bash
cd SensorServer
pip install -r requirements.txt   # inclui o pytest
python -m pytest
```

A configuração está em `SensorServer/pytest.ini`. A suíte tem `tests/unit/` (módulos isolados), `tests/integration/` (rotas HTTP, push SSE, autorização, limite de corpo, banco e análise fora do ar e ciclo de queda ponta a ponta) e `tests/banco/` (o mesmo contrato do repositório no PostgreSQL real; fora da execução padrão: `python -m pytest -m banco`, com o container no ar). Nos testes a análise roda em memória no lugar do processo. Ela é independente do seu ambiente: **não lê o seu `.env`** (`FLOW_SEM_DOTENV=1`), define o próprio `FLOW_JWT_SECRET`, desliga as threads de fundo (`FLOW_SEM_THREADS=1`), usa um banco falso em memória (que também guarda contas e sessões), então **não usa nem altera** as suas contas reais. Três testes saem do padrão de "análise em memória": `tests/integration/test_texto_claro_recusado.py` sobe em loopback a borda (`python -m borda` e `python -m borda.server`) e a análise (`python -m analise`), com certificado de teste, e confere que só respondem por HTTPS; `tests/integration/test_certificado_de_cliente.py` sobe a borda em TLS de verdade, em loopback, e percorre os dois caminhos do certificado de cliente (o app direto e o wokwi com um cliente de teste no papel do bridge: começa sem certificado, emite, usa; certificado de outra conta, de outra CA, vencido, sem `clientAuth`, repasse fora do bridge); e `tests/unit/test_sem_texto_claro.py` varre servidor, bridge, firmware e app atrás de `http://` e `ws://` fora de uma lista de exceções (o `sketch/segredos.h`, cópia local do `segredos.h.example`, fica de fora, como o `local.properties`). Nenhum deles lê o seu `.env` nem usa o seu `certs/`: criam CAs de teste (raiz e de dispositivos) numa pasta temporária.

### Bridge (pytest)

```bash
cd Bridge
pip install -r requirements.txt   # inclui o pytest
python -m pytest
```

A configuração está em `Bridge/pytest.ini`. Os testes não usam rede externa e não leem o seu `.env`: os de `tests/integration/` sobem o bridge real (TLS, loopback, porta livre) contra uma borda falsa, com uma PKI de teste gerada na hora (só `cryptography`). Cobrem a lista fechada de rotas, o certificado de cliente (de outra CA, da raiz, vencido, só `serverAuth` e sem `ExtendedKeyUsage` no handshake), o repasse do certificado do dispositivo no cabeçalho (sempre apagando o que veio do dispositivo) e a identidade própria do bridge em toda chamada à borda (inclusive o SSE), o repasse sem alterar corpo nem cabeçalhos, o SSE em fluxo, as falhas da borda (`502`/`504`, sem retentativa), o reinício do bridge, a conexão persistente e o texto claro recusado. Os de `tests/unit/` conferem a configuração (inclusive a recusa de `http://`), a regra do certificado na API, o repasse e a ausência de estado (nenhum módulo de sessão, tradução ou cifra no bridge).

### App Android (JVM)

Os testes de `SensorApp/app/src/test/` (validação da forma das mensagens de push) rodam na JVM, sem emulador. No Android Studio, clique com o botão direito em `app/src/test` e escolha **Run Tests**; ou rode `gradle test` dentro de `SensorApp`. A primeira execução baixa o JUnit e o `org.json`.

### Fora do pytest (opcionais)

| O quê | Como | Para que serve |
| --- | --- | --- |
<<<<<<< Updated upstream
| `SensorServer/simular_queda.py` | com o servidor rodando: `FLOW_BRIDGE_ID=bridge1 FLOW_BRIDGE_SECRET=<senha> python simular_queda.py [URL_BASE]` (URL padrão `https://127.0.0.1:5000`) (ainda lê essas duas do terminal; não usa o `.env` do bridge) | Simula um wokwi que sofre impacto e fica imóvel; o pedido de confirmação chega ao app e/ou ao bridge conectados, sem Wokwi. O script ainda não pede o certificado de cliente: o login passa, mas a borda recusa a telemetria com `401` (`certificado_ausente`). |
| `python -m adaptadores.software ...` (em `Bridge/`) | ver `docs/arquitetura.md`, seção 14.1 | Dispositivo simulado em software, sem Wokwi, por HTTPS na API do bridge. |
| `sketch/host_test/` | ver `sketch/host_test/README.md` | Roda a lógica do firmware no PC, com um dublê do transporte HTTPS (buzzer, idempotência do `BUZZER_ON`, e a Serial sem segredo nem eventos). |
=======
| `SensorServer/simular_queda.py` | com o servidor rodando: `FLOW_BRIDGE_ID=bridge1 FLOW_BRIDGE_SECRET=<senha> python simular_queda.py [URL_BASE]` (URL padrão `https://127.0.0.1:5000`, a borda; `https://127.0.0.1:5443` vai pelo bridge) (ainda lê essas duas do terminal; não usa o `.env` do bridge) | Simula um wokwi que sofre impacto e fica imóvel: faz login, pede o certificado e envia a telemetria como o firmware; o pedido de confirmação chega ao app conectado, sem Wokwi. |
| `testes_integrados/` | `cd testes_integrados && python -m pytest` (precisa dos `requirements.txt` do servidor e do bridge) | Cliente com o papel do firmware → bridge real → borda real (banco falso), tudo em TLS e com o certificado **exigido** pela borda: login e emissão sem certificado, o resto com certificado; sem certificado, `401 certificado_ausente` vindo da borda; certificado de outra conta, `403 certificado_incompativel`; cabeçalho forjado pelo dispositivo, apagado; push com e sem certificado; wokwi direto na borda sem certificado, `401`. |
| `sketch/host_test/` | ver `sketch/host_test/README.md` (e `verificar_contra_python.py`) | Roda a lógica do firmware no PC contra um servidor falso: envelope AES-GCM (conferido contra `crypto_utils.py`), login e tokens, geração real de chave e CSR (aceito por `borda/emissao.py`), boot e reemissão, leitura do push SSE, buzzer e LEDs, e a Serial sem segredo nem eventos. Não cobre o `transporte_wifi.h` real. |
>>>>>>> Stashed changes

O servidor mantém um **cooldown de 60 s** entre episódios do mesmo usuário (`COOLDOWN_S`): para repetir `simular_queda.py` em seguida, reinicie o servidor ou aguarde.

## Testando manualmente

### Autenticação

Sem token, o servidor recusa:

```bash
curl -i --cacert SensorServer/certs/ca.crt -X POST https://localhost:5000/dados -H "Content-Type: application/json" -d "{}"
```

Esperado: `401`. Com um token válido de app ou wokwi mas sem certificado de cliente, a resposta também é `401`, com `codigo: certificado_ausente`. Tentativas negadas (`401`, `403` e `413`) aparecem no log da borda (`[authz] negado ...`), sem o conteúdo da requisição. Cada certificado emitido gera uma linha no log da borda com serial, conta, `device_id` e validade (nunca o pedido, o certificado nem a chave).

### Leituras no banco

Com servidor, app e bridge rodando, confira:

```bash
docker compose exec -T db psql -U flow -d flow -c "SELECT instante_ingestao, user_id, fonte FROM leituras ORDER BY id DESC LIMIT 5"
```

Cada leitura aceita é uma linha, de `app` ou de `wokwi`, ligada ao usuário da conta. E `curl --cacert SensorServer/certs/ca.crt https://localhost:5000/health` mostra se a análise está respondendo (`analise.estado`: `ok` ou `degradado`; `analise.ultima_resposta_ha_s` diz há quantos segundos ela respondeu pela última vez).

### Queda e confirmação — Wokwi

1. Com a simulação rodando, leve **dois** eixos do acelerômetro a 2 g ao mesmo tempo (por exemplo X e Y, cerca de 27,7 m/s²): o simulador limita cada eixo a 2 g (19,6 m/s²), abaixo do limiar de impacto de 20 m/s², então um eixo sozinho nunca conta como impacto.
2. Zere todos os sliders e mantenha parado por alguns segundos. Zere os dois eixos no mesmo segundo: um eixo que sobra em 2 g conta como movimento e cancela o impacto (**não verificado** no Wokwi). Sem sliders, `SensorServer/simular_queda.py` reproduz o impacto seguido de imobilidade (faz login, pede o certificado e envia a telemetria como o firmware; passe `https://127.0.0.1:5443` para ir pelo bridge, com o Wokwi parado).
3. O servidor deve concluir "queda provável" (impacto seguido de imobilidade) e o **buzzer toca**. Pressione **Estou bem** em até 5 s: o buzzer cala.
4. Se não pressionar, o buzzer cala ao fim do prazo.

Sem impacto algum, deixar tudo zerado por tempo suficiente também dispara o pedido, por **imobilidade prolongada**, mas com mais atraso (o gatilho de impacto tem preferência). O campo `motivo` do registro `episodio.aberto` (tabela `confirmacoes`) mostra qual gatilho abriu o episódio.

Para conferir o ritmo do laço do firmware: depois do login e da emissão do certificado, as linhas `POST /dados` do terminal do bridge devem ficar espaçadas perto de 1 s, e o `eventTimeMs` de leituras consecutivas (consulta de "Leituras no banco") deve diferir perto de 1000 ms (o Wokwi roda um pouco mais lento que o relógio). Numa execução medida, o espaçamento foi de cerca de 1,0 s (o `eventTimeMs` diferiu 1000 ms entre leituras consecutivas), o login (handshake sem certificado de cliente) levou cerca de 3,2 s e as chamadas seguintes, de 64 a 127 ms (medições do ensaio anterior, com o protocolo antigo; os tempos do handshake COM certificado e da emissão **não foram medidos** nesta versão). Depois de uma pausa maior que 10 s, a primeira chamada paga um handshake de novo (medido: cerca de 3,2 s) e o ritmo volta ao normal.

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
| Validade do certificado de cliente do app e do wokwi (padrão 7 dias) | `CERT_DISPOSITIVO_DIAS` em `SensorServer/borda/const.py` (nunca passa da validade da CA de dispositivos) |
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
| Borda não sobe: "CA de dispositivos nao encontrada" (ou "ilegivel") | falta `certs/ca-dispositivos.crt` (ou o caminho de `FLOW_CA_DISPOSITIVOS_CERT`): rode `python scripts/gerar_certificado.py` em `SensorServer/`; ele cria a CA de dispositivos sem tocar na raiz, então ninguém recompila |
| `401` com `codigo: certificado_ausente` em `/dados`, `/eventos` ou `/push/*` | a chamada chegou sem certificado de cliente: o cliente precisa pedir o seu em `/auth/certificado` e repetir numa **conexão nova** com ele. No wokwi, o bridge também precisa repassar o certificado do dispositivo. Não é problema de token: renovar o token ou refazer o login não resolve |
| `403` com `codigo: certificado_incompativel` | o certificado é de outra conta ou de outro `device_id` (troca de conta, ou `device_id` do login diferente do usado na emissão): descarte o certificado e emita outro |
| `503` com `codigo: emissao_indisponivel` em `/auth/certificado` | falta ou está ilegível `certs/ca-dispositivos.key`; o resto da borda segue e os certificados já emitidos continuam valendo |
| `400` com `codigo: csr_invalido` em `/auth/certificado` | o pedido não é um CSR de chave EC P-256 assinado com ECDSA-SHA256, ou a assinatura não confere (erro do cliente; repetir não adianta) |
| Conexão cai no handshake ao apresentar um certificado de cliente | certificado vencido, de outra CA ou sem `clientAuth`. No bridge: `bridge.crt` emitido sem o uso de cliente; reemita com `--forcar --nome bridge --dns host.wokwi.internal` |
| `criar-conta` recusa o identificador | vazio ou com mais de 64 caracteres (limite do certificado de cliente) |
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
