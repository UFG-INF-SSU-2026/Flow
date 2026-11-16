#pragma once
// Duble de ../transporte_wifi.h para o teste no PC (o de verdade precisa do WiFi/TLS do ESP32). Mantem o contrato que o
// firmware enxerga e separa os canais, para o teste conferir o que sai pela "rede" e o que sai pela Serial:
//   g_rede_eventos   o que o firmware mandaria ao bridge em POST /eventos e /dados (HTTPS)
//   g_rede_login     o que mandaria em POST /v1/login (HTTPS): e o unico lugar onde o segredo pode aparecer
//   bridge_envia()   o que o bridge entregaria em GET /comandos (uma linha por comando)
#include "Arduino.h"
#include <string>
#include <deque>

extern const char* DEVICE_ID;
void receberComando(const char* texto, size_t tamanho);   // sketch.ino
void emitirLogin();                                       // sketch.ino

static std::string g_rede_eventos, g_rede_login;
static std::deque<std::string> g_rede_comandos;
static int g_wifi_iniciado = 0;

inline void bridge_envia(const std::string& linhas) {
  size_t ini = 0;
  while (ini < linhas.size()) {
    size_t fim = linhas.find('\n', ini);
    if (fim == std::string::npos) fim = linhas.size();
    g_rede_comandos.push_back(linhas.substr(ini, fim - ini));
    ini = fim + 1;
  }
}

void wifiIniciar() { g_wifi_iniciado++; }
void wifiLogin(const char* json) { g_rede_login += json; g_rede_login += "\n"; }
void wifiEnviarEvento(const char* json) { g_rede_eventos += json; g_rede_eventos += "\n"; }
void wifiBuscarComandos() {
  while (!g_rede_comandos.empty()) {
    std::string l = g_rede_comandos.front(); g_rede_comandos.pop_front();
    receberComando(l.c_str(), l.size());
  }
}
