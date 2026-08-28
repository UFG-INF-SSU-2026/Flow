# Projeto: Coleta de Dados de Sensores

Este projeto tem duas partes:

1. **SensorServer/** — servidor Python (Flask) que roda no seu computador.
2. **SensorApp/** — projeto Android Studio (Kotlin) que gera o APK a ser
   instalado no celular.

---

## 1. Servidor (SensorServer)

### Passo a passo

```bash
cd SensorServer
pip install -r requirements.txt
python server.py
```

O servidor vai escutar em `http://0.0.0.0:5000` (local, porta 5000) e salvar cada requisição
recebida como um arquivo `.json` dentro da pasta `dataUsers/`, com o nome:

```text
<device_id>_<timestamp_recebimento>.json
```

### Como descobrir o IP do seu computador (necessário para o app)

- **Windows**: `ipconfig` (procure "Endereço IPv4")
- **Mac/Linux**: `ifconfig` ou `ip a` (procure algo como `192.168.x.x`)

Anote esse IP — você vai precisar dele no passo 2.

### Variáveis editáveis (topo do `server.py`)

- Obs.: Você precisa alterar essas variáveis APENAS se a porta 5000 já estiver sendo usada em seu computador, ou se vc quiser salvar num local distinto de Data_dir. Em caso contrário, nem toque.
- `PORT` — porta do servidor (padrão: 5000)
- `DATA_DIR` — pasta onde os JSONs são salvos (padrão: "dataUsers")

---

## 2. Aplicativo Android (SensorApp)

### Passo a passo

Por esse método, você precisa gera o APK pelo aplicativo Android Studio. É mais versátil pois você pode alterar funcionalidades se assim desejar.

  1. Abra o **Android Studio**.
  2. Escolha **"Open"** e selecione a pasta `SensorApp/`.
  3. Aguarde o Gradle sincronizar (primeira vez pode demorar alguns minutos,
    pois ele baixa o Gradle e as dependências).
  4. **Antes de gerar o APK**, edite o arquivo:
    `app/src/main/java/com/example/sensorapp/MainActivity.kt`
    No topo da classe, você pode ajustar (opcional):

    ```kotlin
    private val SEND_INTERVAL_MS: Long = 10_000L
    private val DEFAULT_SERVER_IP: String = "192.168.0.10"
    private val DEFAULT_SERVER_PORT: String = "5000"
    ```

    - `DEFAULT_SERVER_IP` / `DEFAULT_SERVER_PORT` são usados apenas como valor
      inicial. **O IP e a porta reais agora podem ser configurados direto no
      app**, pelo botão "Socket de Rede" (veja abaixo) — não é mais
      obrigatório editar o código para isso.
    - Se quiser mudar o intervalo de envio no futuro, basta alterar o valor
      de `SEND_INTERVAL_MS` (em milissegundos) e gerar o APK novamente.

  5. No menu do Android Studio: **Build > Build App Bundle(s) / APK(s) > Build APK(s)**.
  6. Quando terminar, clique em **"locate"** no aviso que aparece (ou procure em
    `app/build/outputs/apk/debug/app-debug.apk`).
  7. Transfira esse `.apk` para o celular (cabo USB, e-mail, etc.) e instale
    (pode ser necessário permitir "instalar de fontes desconhecidas").

### Como o app funciona

- Ao abrir, mostra o endereço do servidor configurado (ou o padrão definido
  no código, na primeira vez), o status "Parado" e um botão **"Iniciar"**.
- **Botão "Socket de Rede"**: abre uma caixa de diálogo para digitar o IP e
  a porta do servidor. Ao clicar em "Salvar", o valor fica gravado no
  próprio celular (persiste mesmo se você fechar o app ou reiniciar o
  telefone) e passa a ser usado em todos os envios seguintes. Se o app
  estiver enviando dados no momento em que você altera o endereço, o envio
  é parado automaticamente (você precisa clicar em "Iniciar" de novo).
- Ao tocar em "Iniciar":
  - Registra automaticamente **todos os sensores disponíveis** no aparelho
    (acelerômetro, giroscópio, magnetômetro, luz, proximidade, pressão etc.
    — **GPS não está incluso**, conforme definido).
  - A cada `SEND_INTERVAL_MS` (10s por padrão), envia um JSON com a última
    leitura de cada sensor para o `SERVER_URL`.
  - Mostra na tela se está "Conectado" ou se o servidor está indisponível
    (nesse caso, apenas ignora e tenta de novo no próximo ciclo).
- Ao tocar em "Parar", ou **ao minimizar/sair do app**, o envio para
  automaticamente (funciona apenas em primeiro plano, como definido).
- O identificador do dispositivo (`device_id`) usado nos arquivos JSON é o
  `ANDROID_ID` do aparelho (um identificador único por instalação/dispositivo).

### Observação sobre rede

O app se comunica via **HTTP simples (sem HTTPS)**, por isso o
`AndroidManifest.xml` já inclui `usesCleartextTraffic="true"`. Certifique-se
de que o celular esteja conectado à **mesma rede Wi-Fi** do computador que
roda o servidor.

---

## Estrutura de um JSON recebido pelo servidor (exemplo)

```json
{
  "device_id": "a1b2c3d4e5f6",
  "device_model": "SM-G975F",
  "timestamp_envio": 1735000000000,
  "sensores": {
    "1": {
      "nome": "LSM6DS3 Accelerometer",
      "valores": [0.12, 9.81, 0.03],
      "timestamp_leitura": 123456789
    },
    "4": {
      "nome": "LSM6DS3 Gyroscope",
      "valores": [0.0, 0.01, -0.02],
      "timestamp_leitura": 123456790
    }
  }
}
```

(O número da chave dentro de `"sensores"` corresponde ao tipo do sensor
definido pela API Android — ex: `1` = acelerômetro, `4` = giroscópio.)
