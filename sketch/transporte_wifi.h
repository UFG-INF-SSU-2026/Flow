#pragma once
// EXPERIMENTAL e DESLIGADO por padrao: o ESP32 fala HTTPS direto com a API do bridge, sem serial nem adaptador.
// Ligue com "#define FLOW_TRANSPORTE_WIFI" em segredos.h (ver segredos.h.example). Nao foi compilado nem rodado no Wokwi
// pelo autor: so o caminho serial e testado (sketch/host_test).
//
// O que precisa ser verdade para funcionar:
//  - O ESP32 do Wokwi gratuito so alcanca a internet publica. Para chegar ao bridge no seu PC e preciso o Private Gateway
//    do Wokwi (plano pago): ele expoe o PC como host.wokwi.internal.
//  - Chaves: o bridge tem a chave PRIVADA e o certificado da API (emitido pela CA do projeto); o firmware leva so a chave
//    PUBLICA da CA (FLOW_CA_PEM) e confere o certificado. O nome da URL precisa estar no certificado:
//    python scripts/gerar_certificado.py --forcar --nome bridge --dns host.wokwi.internal (em SensorServer/).
//  - O certificado tem prazo: o ESP32 precisa do horario certo (NTP) antes do handshake; sem ele a verificacao falha.
//  - Este codigo NUNCA desliga a verificacao (nada de setInsecure).
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>

extern const char* DEVICE_ID;
void processarComando(char* linha);   // sketch.ino
void emitirLogin();                   // sketch.ino

static WiFiClientSecure g_wifiCli;
static HTTPClient g_wifiHttp;
static String g_wifiToken;
static unsigned long g_wifiProximoPoll = 0;

static String wifiUrl(const String& rota) { return String(FLOW_BRIDGE_URL) + rota; }

// POST JSON. Devolve o codigo HTTP (ou -1) e, em *resp, o corpo.
static int wifiPost(const String& rota, const char* json, String* resp = nullptr) {
  if (WiFi.status() != WL_CONNECTED || !g_wifiHttp.begin(g_wifiCli, wifiUrl(rota))) return -1;
  g_wifiHttp.addHeader("Content-Type", "application/json");
  if (g_wifiToken.length()) g_wifiHttp.addHeader("Authorization", "Bearer " + g_wifiToken);
  int code = g_wifiHttp.POST((uint8_t*)json, strlen(json));
  if (resp && code > 0) *resp = g_wifiHttp.getString();
  g_wifiHttp.end();
  return code;
}

void wifiIniciar() {
  WiFi.begin("Wokwi-GUEST", "", 6);                       // canal 6 acelera a conexao no Wokwi
  for (int i = 0; i < 100 && WiFi.status() != WL_CONNECTED; i++) delay(200);
  configTime(0, 0, "pool.ntp.org", "time.google.com");    // horario para validar o prazo do certificado
  for (int i = 0; i < 50 && time(nullptr) < 1700000000; i++) delay(200);
  g_wifiCli.setCACert(FLOW_CA_PEM);                        // so a CA do projeto
  g_wifiHttp.setReuse(true);
  Serial.println(WiFi.status() == WL_CONNECTED ? "WiFi conectado" : "WiFi NAO conectou");
}

// Login: o corpo e a mesma linha de login da serial; o bridge guarda so o token local da resposta.
void wifiLogin(const char* json) {
  g_wifiToken = "";
  String resp;
  int code = wifiPost("/v1/login", json, &resp);
  int i = resp.indexOf("\"token\":\"");
  if (code == 200 && i >= 0) {
    i += 9;
    g_wifiToken = resp.substring(i, resp.indexOf('"', i));
    Serial.println("bridge: login aceito");
  } else {
    Serial.printf("bridge: login nao aceito (HTTP %d)\n", code);
  }
}

void wifiEnviarEvento(const char* json) {
  if (!g_wifiToken.length()) return;
  int code = wifiPost(String("/v1/dispositivos/") + DEVICE_ID + "/eventos", json);
  if (code == 401) { g_wifiToken = ""; emitirLogin(); }   // sessao expirada: o bridge nao guarda o segredo, mandamos de novo
}

// A cada 1 s: busca os comandos prontos e entrega cada um a processarComando (o mesmo parser da serial).
void wifiBuscarComandos() {
  if (!g_wifiToken.length() || millis() < g_wifiProximoPoll) return;
  g_wifiProximoPoll = millis() + 1000;
  if (!g_wifiHttp.begin(g_wifiCli, wifiUrl(String("/v1/dispositivos/") + DEVICE_ID + "/comandos"))) return;
  g_wifiHttp.addHeader("Authorization", "Bearer " + g_wifiToken);
  int code = g_wifiHttp.GET();
  String corpo = code == 200 ? g_wifiHttp.getString() : String();
  g_wifiHttp.end();
  if (code == 401) { g_wifiToken = ""; emitirLogin(); return; }
  int i = 0;                                              // corpo: {"comandos":["CMD;...","CMD;..."]}
  while ((i = corpo.indexOf("\"CMD;", i)) >= 0) {
    int fim = corpo.indexOf('"', i + 1);
    if (fim < 0) break;
    char linha[96];
    corpo.substring(i + 1, fim).toCharArray(linha, sizeof(linha));
    processarComando(linha);
    i = fim + 1;
  }
}
