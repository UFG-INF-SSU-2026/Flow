# host_test (opcional)

Teste da lógica do firmware no PC, com um "Arduino falso" e um **dublê do transporte HTTPS** (`transporte_wifi.h` desta
pasta, no lugar do `../transporte_wifi.h`, que precisa do WiFi/TLS do ESP32). O dublê separa o que sairia pela rede (eventos
e login, para o bridge) do que sai pela Serial, e o teste confere que a Serial nunca carrega evento, comando nem segredo.
Não faz parte do build do Arduino/Wokwi.

```
cp ../sketch.ino sketch_host.cpp
g++ -std=c++17 -I. -o harness harness.cpp && ./harness
```

Esperado: 55 linhas `PASS` e `FIRMWARE OK 0`. Apague `sketch_host.cpp` e `harness` depois.

O que este teste **não** cobre: o próprio `../transporte_wifi.h` (handshake TLS, NTP, relogin), que só roda no ESP32/Wokwi.
