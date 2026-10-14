# host_test (opcional)

Teste da lógica do firmware no PC, com um "Arduino falso". Não faz parte do build do Arduino/Wokwi.

```
cp ../sketch.ino sketch_host.cpp
g++ -std=c++17 -I. -o harness harness.cpp && ./harness
```

Esperado: 18 linhas `PASS` e `FIRMWARE OK 0`. Apague `sketch_host.cpp` e `harness` depois.
