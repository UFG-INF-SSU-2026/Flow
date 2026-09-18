# Flow — Monitoramento Ubíquo de Idosos

O Flow é um sistema de monitoramento de pessoas idosas. Sensores próximos ao
idoso produzem dados que são recebidos e processados por um servidor central
e, quando necessário, geram um alerta simulando a notificação do cuidador.

O sistema tem duas fontes de dados independentes, que podem pertencer à
mesma pessoa monitorada:

- **App Android** (`SensorApp/`), instalado no celular do idoso, que
  coleta leituras dos sensores do próprio aparelho (incluindo luminosidade
  ambiente).
- **Wokwi** (`Sketch/`), a simulação de um ESP32 com acelerômetro MPU6050
  que detecta imobilidade do idoso, encaminhada ao servidor por uma ponte
  Python (`WokwiBridge/`).

Ambas as fontes enviam dados **a cada 1 segundo** para um **único servidor**
(`SensorServer/`), que os associa por usuário e os armazena.

## Arquitetura

```
 App Android  ──┐
 (SensorApp)    │  HTTP POST /dados (envelope cifrado)
                ├──────────────────────────────────►  SensorServer
 Wokwi ESP32    │  HTTP POST /dados (envelope cifrado)   (Flask)
 (Sketch) ──► WokwiBridge ──┘
   simulação      (bridge.py)
```

- O **Sketch** roda dentro da extensão Wokwi do VS Code e simula um
  dispositivo vestível com acelerômetro (MPU6050) que decide, por
  histerese de dois limiares, se o idoso está em estado normal ou em
  possível imobilidade.
- O **WokwiBridge** lê a saída serial dessa simulação (via RFC2217, recurso
  de depuração da extensão Wokwi), cifra cada evento e o envia por HTTP ao
  servidor — o Wokwi simulado não fala HTTP diretamente, por isso a ponte é
  necessária.
- O **SensorServer** é o único ponto de recepção: tanto o app quanto o
  bridge enviam para o mesmo endpoint `POST /dados`.

## Identificação de usuário

Cada envio, de qualquer uma das duas fontes, carrega um `user_id` (número
inteiro). Dados de fontes diferentes com o mesmo `user_id` são entendidos
como pertencentes à mesma pessoa e são associados no armazenamento.

- No **app**, o `user_id` é configurado pela tela **Configurações**, junto
  com o IP e a porta do servidor, e fica salvo no celular.
- No **wokwi**, o `user_id` é a constante `WOKWI_USER_ID`, definida no topo
  de `WokwiBridge/bridge.py`.

Para que os dados das duas fontes sejam entendidos como do mesmo idoso, os
dois valores precisam ser configurados com o **mesmo número**.

## Transporte e criptografia

Toda mensagem enviada ao servidor — de qualquer fonte — é cifrada com
AES-256-GCM usando uma chave compartilhada fixa (`crypto_utils.py` no
servidor e no bridge; `CryptoUtils.kt` no app) e transportada como:

```json
{ "nonce": "...", "ciphertext": "..." }
```

O servidor decifra esse envelope da mesma forma para as duas fontes. O
conteúdo decifrado, porém, não segue um schema único entre elas — cada
fonte envia sua própria estrutura de JSON, compartilhando apenas dois
campos de roteamento:

- `user_id` (inteiro): identifica o idoso dono do dado.
- `source` (`"app"` ou `"wokwi"`): identifica de qual fonte o dado veio.

**Payload do app** inclui, além de `user_id` e `source`: `device_id`,
`device_model`, `timestamp_envio`, `sensores` (mapa com as leituras dos
sensores do celular) e, quando disponível, `leitura_ambiente` (evento
estruturado de luminosidade usado na regra de alerta).

**Payload do wokwi** (montado pelo bridge a partir do que o sketch imprime
na serial) inclui, além de `user_id` e `source`: `eventType`
(`imobilidade.leitura`, emitido a cada segundo, ou `imobilidade.decisao`,
emitido só em transições de estado), `deviceId`, `entityId`, `eventTimeMs`
(relógio interno do ESP32 simulado, não um horário absoluto), `sequence`,
`value`, `unit` e `state`.

## Armazenamento

O servidor mantém, em memória, a leitura mais recente de cada fonte para
cada `user_id`. A cada 1 segundo, uma linha é gravada em
`SensorServer/dataUsers/usuario_<user_id>.jsonl`, combinando o que chegou
de cada fonte naquela janela:

```json
{
  "timestamp": "2026-09-17T22:48:03.061334+00:00",
  "user_id": 1,
  "app": { "device_id": "...", "sensores": { "...": "..." } },
  "wokwi": { "eventType": "imobilidade.leitura", "value": 0.12, "state": "NORMAL" }
}
```

Se, numa determinada janela de 1 segundo, apenas uma das fontes enviou
dado, a chave da outra fica `null`. Se nenhuma das duas enviou nada naquele
segundo, nenhuma linha é gravada.

## Regra de alerta

Os dados de `leitura_ambiente` do app passam por uma regra de luz
inadequada (`regra_luz.py`): quando a condição de alerta é atingida, o
servidor grava um arquivo `alerta_<device_id>_<timestamp>.json` em
`dataUsers/`, simulando a notificação ao cuidador. Esse fluxo é
independente da agregação por janela de 1s descrita acima e roda assim que
o payload do app chega.

Os eventos `imobilidade.decisao` vindos do wokwi são recebidos, validados
no envelope (fonte e usuário) e armazenados normalmente dentro da chave
`"wokwi"` do jsonl do usuário, junto com os demais eventos daquela fonte.

## Estrutura do projeto

```
Flow-Unificado/
├── SensorServer/      # servidor Flask único (recebe app + wokwi)
│   ├── server.py
│   ├── crypto_utils.py
│   ├── regra_luz.py
│   ├── requirements.txt
│   └── dataUsers/      # arquivos .jsonl por usuário e alertas gerados
├── SensorApp/          # app Android (Kotlin)
├── WokwiBridge/         # ponte entre a simulação Wokwi e o servidor
│   ├── bridge.py
│   ├── crypto_utils.py
│   └── requirements.txt
├── Sketch/              # simulação Wokwi (ESP32 + MPU6050, detecção de imobilidade)
│   ├── sketch.ino
│   ├── diagram.json
│   └── wokwi.toml
└── INSTRUCOES.md
```

## App Android

O APK já compilado está disponível na página de *releases* do repositório
no GitHub do projeto.

## Endpoints do servidor

| Método | Rota      | Descrição                                                              |
|--------|-----------|-------------------------------------------------------------------------|
| POST   | `/dados`  | Recebe o envelope cifrado de uma das duas fontes (app ou wokwi).        |
| GET    | `/dados`  | Lista, em memória, os últimos registros já agregados (uso de depuração).|
| GET    | `/health` | Verificação simples de que o servidor está no ar.                       |
| GET    | `/`       | Status geral do servidor.                                               |
