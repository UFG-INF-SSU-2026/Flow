# host_test (opcional)

Teste da lógica do firmware no PC, com um "Arduino falso" e um **dublê do transporte** (`transporte_wifi.h` desta pasta, no lugar do
`../transporte_wifi.h`, que precisa do WiFi/TLS do ESP32). O dublê é um **servidor falso programável**: recebe o que o firmware enviaria
(já decifrado com o `envelope.h` **real** do firmware), responde login, emissão de certificado, refresh e `/dados`, e entrega bytes de
SSE em chunks. Assim o teste cobre o que roda no PC: envelope AES-GCM, login, tokens e renovação, geração real de chave e CSR (mbedTLS),
a ordem do boot, falha fechada sem certificado, reemissão após falhas, leitura do push (chunks, SSE, validação, deduplicação,
reconexão e backoff), buzzer, LEDs e a máquina de imobilidade. Não faz parte do build do Arduino/Wokwi.

Dependências no PC (Linux/WSL/macOS): `g++`, mbedTLS (`libmbedtls-dev`) e a biblioteca ArduinoJson (só os cabeçalhos, v7):

```
git clone --depth 1 --branch v7.4.2 https://github.com/bblanchon/ArduinoJson /tmp/ArduinoJson
cd sketch/host_test
cp ../sketch.ino sketch_host.cpp
g++ -std=c++17 -I. -I.. -I/tmp/ArduinoJson/src -o harness harness.cpp -lmbedcrypto -lmbedx509 && ./harness
```

Esperado: só linhas `PASS` e `FIRMWARE OK 0`. Apague `sketch_host.cpp` e `harness` depois (estão no `.gitignore`).

## Conferência contra o servidor Python

Com o harness compilado e `SensorServer/requirements.txt` instalado:

```
python verificar_contra_python.py
```

Confere o formato do envelope nas duas direções (o texto cifrado do firmware, com o mesmo nonce, é igual ao do Python) e que o CSR
gerado pelo firmware é aceito por `borda/emissao.py` (a mesma validação da rota `POST /auth/certificado`) e vira certificado.

## O que este teste **não** cobre

- O próprio `../transporte_wifi.h` (handshake TLS com e sem certificado de cliente, NTP, chunks reais do bridge, `HTTPClient`):
  só roda no ESP32/Wokwi. **Nada disso foi executado**; veja o roteiro manual em `INSTRUCOES.md`.
- A API do mbedTLS 3.x do core do ESP32: aqui o teste usa a do PC (2.28). `certificado.h` usa só funções comuns às duas, mas
  a compilação para o ESP32 não foi verificada.
- Tempos reais de handshake e consumo de RAM.
