# Operação: detalhes técnicos

Complemento do [`INSTRUCOES.md`](../INSTRUCOES.md): o que cada passo faz por baixo, como ajustar e como resolver os
problemas. As decisões de projeto estão em [`arquitetura.md`](arquitetura.md); a lista completa de variáveis de
configuração, na seção 14 dela.

## Certificados

### O que existe

Tudo é gerado por `SensorServer/scripts/gerar_certificado.py` (o `scripts/preparar.*` o chama) em `SensorServer/certs/`,
que fica fora do Git.

| Arquivo | O que é | Quem usa |
| --- | --- | --- |
| `ca.crt`, `ca.key` | CA do projeto. Assina os certificados de serviço. A chave só o script lê. | Todos confiam no `ca.crt` |
| `borda.*`, `analise.*`, `banco.*` | Certificado e chave de cada serviço | O próprio serviço |
| `bridge.*` | Certificado do bridge (`--nome bridge`): serve a API dele e o identifica como cliente na borda (`serverAuth` e `clientAuth`, `CN=bridge`) | Bridge |
| `ca-dispositivos.crt`, `ca-dispositivos.key` | CA de dispositivos, independente da raiz. Assina os certificados de cliente do app e do ESP32. | A chave só a borda lê; o bridge lê o `.crt` |

O script também copia o `ca.crt` para `SensorApp/app/src/main/res/raw/flow_cert.pem` (cópia opcional embutida no app)
e gera `sketch/ca_flow.h` (o mesmo certificado público, para o firmware). Os dois ficam fora do Git.

### Quando reemitir

| Situação | O que fazer | Quem precisa mudar algo |
| --- | --- | --- |
| O computador mudou de rede (IP novo) | Nada além de `scripts/preparar.sh`: o script percebe o IP novo e reemite só o certificado da borda. Depois, suba a borda de novo. | Ninguém (a CA é a mesma); no app, troque o IP |
| Acrescentar um endereço fixo | `python scripts/gerar_certificado.py --forcar --ip <IP>` (ou `--dns <nome>`), depois `docker compose restart db` e reinicie borda e análise | Ninguém |
| Trocar a CA (`--forcar-ca`) | Reemite tudo com uma CA nova | App: busca a CA nova no próximo login. Firmware: recompilar (`ca_flow.h`). Bridge: reemitir com `--nome bridge --dns host.wokwi.internal`. Os certificados de cliente continuam valendo (são da CA de dispositivos) |
| Trocar a CA de dispositivos | Apague `certs/ca-dispositivos.*` e rode o script (nem `--forcar` nem `--forcar-ca` a recriam) | Os dispositivos pedem certificado novo sozinhos |

O certificado da borda vale para `localhost`, `127.0.0.1`, `::1`, o IP de rede detectado e os de `--ip`/`--dns`. Quem
acessa por outro endereço tem a conexão recusada.

### Certificado de cliente dos dispositivos

Cada dispositivo começa sem certificado. Depois do login, gera um par de chaves EC P-256 no próprio aparelho e envia só
o pedido (CSR) a `POST /auth/certificado`. A borda devolve um certificado de 7 dias com a conta e o `device_id` **do
token**. A partir daí:

- login, refresh e emissão passam sem certificado;
- `/dados`, `/eventos`, o push e as rotas do cuidador exigem um certificado cuja conta e `device_id` sejam os do token;
- o app apresenta o certificado direto à borda; o ESP32 o apresenta ao bridge, que o confere e o repassa à borda no
  cabeçalho `X-Flow-Certificado-Cliente`, numa conexão em que o bridge se identifica com o `bridge.crt`.

Cada certificado emitido gera uma linha no log da borda com serial, conta, `device_id` e validade.

## Servidor

### Dois processos

| Processo | Comando (em `SensorServer/`) | Lê do `.env` | Porta |
| --- | --- | --- | --- |
| Borda | `python -m borda` (ou `scripts/borda.*`) | tudo | 5000, em todas as interfaces |
| Análise | `python -m analise` (ou `scripts/analise.*`) | só `FLOW_ANALISE_*` e `FLOW_TLS_*` | 5100, só em `127.0.0.1` |

`scripts/tudo.*` sobe os dois; a ordem entre eles é indiferente. `FLOW_BORDA_HOST=127.0.0.1` restringe a borda ao
próprio computador. Ctrl+C faz o desligamento ordenado: a borda para de aceitar, fecha os streams de push (os clientes
reconectam) e grava o que estava na fila.

Sem a análise, a borda continua aceitando e gravando, mas nada é decidido (`/health` mostra `degradado`). Quando a
análise volta, a borda manda o estado de volta a partir do banco e reenvia a fila.

Sem Docker, só para ver as rotas: `FLOW_DB_FAKE=1 python -m borda.server` (banco em memória; os dados somem ao parar).

### Contas

- `idoso` e `dispositivo` têm exatamente um `--user-id`; `cuidador` tem um ou mais (as pessoas que acompanha).
- `dispositivo` lista os `--device-id` que o login pode usar (repita a opção para vários). O do firmware é o `DEVICE_ID`
  de `sketch/sketch.ino`.
- O identificador da conta tem no máximo 64 caracteres (vai no certificado de cliente).
- Para criar sem o prompt de senha: `FLOW_SENHA=<senha> python -m borda.auth criar-conta ...`.
- As contas ficam no banco. Recriar o volume (`docker compose down -v`) apaga as contas junto.

### Banco

Tabelas, retenção, backup e consultas úteis: [`banco.md`](banco.md). Consulta rápida do que o servidor guarda de cada
episódio:

```bash
docker compose exec -T db psql -U flow -d flow -c "SELECT instante, tipo, dados->>'resultado' AS resultado FROM confirmacoes WHERE user_id = 1 ORDER BY id"
```

## Compilar o app

Precisa de JDK 21 (o Gradle 8.7 do projeto não roda no Java 25) e do Android SDK com a API 34. Sem o Android Studio,
basta instalar as ferramentas de linha de comando:

```bash
# Ferramentas de linha de comando do Android (versão atual em developer.android.com/studio)
mkdir -p ~/Android/Sdk/cmdline-tools && cd ~/Android/Sdk/cmdline-tools
wget https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip
unzip -q commandlinetools-linux-*_latest.zip && mv cmdline-tools latest
export JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
~/Android/Sdk/cmdline-tools/latest/bin/sdkmanager --licenses
~/Android/Sdk/cmdline-tools/latest/bin/sdkmanager "platform-tools" "platforms;android-34" "build-tools;34.0.0"

# No projeto
cd SensorApp
echo "sdk.dir=$HOME/Android/Sdk" > local.properties    # fora do Git
./gradlew assembleDebug    # APK em app/build/outputs/apk/debug/
./gradlew test             # testes na JVM
```

Com o Android Studio, basta abrir a pasta `SensorApp/`.

## Bridge e ESP32 simulado

### Bridge

O bridge não tem conta nem token: só repassa as chamadas do firmware. Configuração em `Bridge/.env` (todas têm padrão):

| Chave | Padrão | Para quê |
| --- | --- | --- |
| `FLOW_SERVIDOR` | `https://127.0.0.1:5000` | Endereço da borda (precisa ser `https://`) |
| `FLOW_TLS_CA` | `../SensorServer/certs/ca.crt` | CA em que o bridge confia para falar com a borda |
| `FLOW_CA_DISPOSITIVOS_CERT` | `../SensorServer/certs/ca-dispositivos.crt` | Confere o certificado de cliente do firmware |
| `FLOW_BRIDGE_HOST`, `FLOW_BRIDGE_PORT` | `127.0.0.1`, `5443` | Onde a API do bridge escuta |
| `FLOW_BRIDGE_API_CERT`, `FLOW_BRIDGE_API_KEY` | `../SensorServer/certs/bridge.crt`, `bridge.key` | Certificado da API e identidade na borda |

Cada chamada gera uma linha no terminal (`[bridge] HH:MM:SS.mmm METODO /caminho STATUS`), sem corpo nem cabeçalhos. No
Windows, use `127.0.0.1` em vez de `localhost` em `FLOW_SERVIDOR` (o `localhost` tenta IPv6 primeiro e cada conexão
nova espera 1 a 2 s). Se o `wokwigw` não alcançar `127.0.0.1`, use `FLOW_BRIDGE_HOST=0.0.0.0` e inclua o IP do PC no
certificado do bridge (`--ip`).

### Firmware

Compilação, dependências, `wokwigw`, circuito, como simular uma queda e problemas: [`wokwi.md`](wokwi.md).

## Ajustes comuns

| O que ajustar | Onde |
| --- | --- |
| Prazo do botão "Estou bem" (padrão 5 s) | `TIMEOUT_CONFIRMACAO_S` em `SensorServer/comum/const.py` (vale para app e ESP32, sem recompilar) |
| Limiares de impacto e de repouso | `LIMIAR_IMPACTO` e `LIMIAR_REPOUSO` em `SensorServer/analise/const.py` |
| Intervalo mínimo entre episódios da mesma pessoa (padrão 10 s) | `COOLDOWN_S` em `SensorServer/analise/const.py` |
| Horário da noite para a regra de luz | `HORA_INICIO_NOITE` e `HORA_FIM_NOITE` em `SensorServer/analise/const.py` |
| Validade dos tokens | `ACCESS_TTL_S` e `REFRESH_TTL_S` em `SensorServer/borda/const.py` |
| Validade do certificado de cliente (padrão 7 dias) | `CERT_DISPOSITIVO_DIAS` em `SensorServer/borda/const.py` |
| Tamanho máximo do corpo (padrão 64 KiB) | `MAX_CONTENT_LENGTH` em `SensorServer/borda/const.py` |
| Intervalo de envio do app (padrão 1 s) | `SEND_INTERVAL_MS` em `SensorApp/.../MainActivity.kt` |

## Problemas comuns

| Sintoma | Causa provável e solução |
| --- | --- |
| Borda não sobe: segredo | `FLOW_JWT_SECRET` ausente ou curto (mínimo 32 bytes) no `.env`. Ao subir, o servidor mostra se leu o `.env` |
| Borda ou análise não sobem: "certificado TLS nao encontrado" | Falta o par do serviço ou o `ca.crt`: rode `scripts/preparar.sh` |
| Borda não sobe: "CA de dispositivos nao encontrada" | Rode `scripts/preparar.sh`: ele cria a CA de dispositivos sem tocar na raiz |
| Banco não sobe, ou a borda não conecta depois de reemitir certificados | O container ainda tem o par antigo: `docker compose restart db` (se não bastar, `docker compose up -d --force-recreate db`; os dados ficam) |
| App: falha de conexão | IP ou porta errados, redes diferentes ou firewall bloqueando a porta 5000 |
| App: erro de certificado | O IP digitado não está no certificado da borda: use o IP que a borda mostrou ao subir |
| App: "tipo de conta ainda não suportado" | O app aceita contas `idoso` e `cuidador` |
| Login recusado com `401` | Usuário ou senha errados (no firmware: `FLOW_IDENTIFICADOR` e `FLOW_SEGREDO` em `segredos.h`) |
| Login recusado com `429` | Bloqueio temporário depois de 5 falhas em 15 minutos |
| Login do firmware recusado com `403 device_nao_permitido` | O `DEVICE_ID` de `sketch.ino` não está na lista da conta: recrie a conta com `--device-id <o do firmware>` |
| `401 certificado_ausente` em `/dados`, `/eventos` ou no push | A chamada chegou sem certificado de cliente. App e firmware pedem um sozinhos; no Wokwi, confira se o bridge está repassando. Renovar o token não resolve |
| `403 certificado_incompativel` | Certificado de outra conta ou de outro `device_id`: o dispositivo emite outro sozinho |
| `503 emissao_indisponivel` em `/auth/certificado` | Falta `certs/ca-dispositivos.key`; certificados já emitidos continuam valendo |
| Bridge não sobe por causa do certificado | `bridge.crt` sem `clientAuth` ou sem `CN=bridge`: `python scripts/gerar_certificado.py --forcar --nome bridge --dns host.wokwi.internal` |
| Problemas do Wokwi (compilação, `login nao aceito`, buzzer) | Ver [`wokwi.md`](wokwi.md#problemas-comuns) |
| O pedido de confirmação não abre de novo logo depois de um teste | Intervalo mínimo de 10 s entre episódios (`COOLDOWN_S`) |
| `413` | Corpo acima de 64 KiB; o servidor nem processa o conteúdo |
