#pragma once
// UNICO transporte do firmware: HTTPS direto com a API do bridge (Bridge/api.py). Nao ha serial nem adaptador: o ESP32 simulado
// fala TLS 1.2+ com o bridge pelo WiFi do Wokwi, confere o certificado dele contra a CA do projeto e NUNCA desliga essa
// verificacao (nada de setInsecure).
//
// O que precisa ser verdade para funcionar (passo a passo em INSTRUCOES.md, secao 2):
//  - O ESP32 do Wokwi so alcanca o PC pelo gateway de rede do Wokwi (host.wokwi.internal). Em Wokwi para VS Code, rode o
//    wokwigw e mantenha o "[net] gateway" do wokwi.toml (sketch/wokwi.toml).
//  - O bridge tem a chave PRIVADA e o certificado da API (emitido pela CA do projeto); o firmware leva so a chave PUBLICA da
//    CA (FLOW_CA_PEM, em segredos.h) e o nome da URL precisa constar no certificado:
//      python scripts/gerar_certificado.py --forcar --nome bridge --dns host.wokwi.internal      (em SensorServer/)
//  - O certificado tem prazo: o ESP32 precisa do horario certo (NTP) antes do handshake; sem ele a verificacao falha.
//
// Sem fila de retransmissao (decisao de arquitetura): se o bridge nao responde, o evento e descartado e a leitura seguinte
// prevalece. Depois de uma falha de conexao a rede fica em silencio por alguns segundos, para o laco principal (botoes,
// buzzer) nao ficar preso esperando o prazo da conexao a cada volta.
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>

#ifndef FLOW_BRIDGE_URL
#define FLOW_BRIDGE_URL "https://host.wokwi.internal:5443"
#endif

// Garantia em tempo de compilacao: a URL do bridge tem de ser https://. Uma URL http:// nem compila.
constexpr bool flowUrlEhHttps(const char* s) {
  return s[0] == 'h' && s[1] == 't' && s[2] == 't' && s[3] == 'p' && s[4] == 's' && s[5] == ':' && s[6] == '/' && s[7] == '/';
}
static_assert(flowUrlEhHttps(FLOW_BRIDGE_URL), "FLOW_BRIDGE_URL precisa comecar com https:// (toda ponta fala TLS)");

extern const char* DEVICE_ID;
void receberComando(const char* texto, size_t tamanho);   // sketch.ino: valida o tamanho e entrega a processarComando
void emitirLogin();                                       // sketch.ino

static const unsigned long WIFI_TIMEOUT_MS      = 3000;   // conexao e resposta; curto de proposito (ver acima)
static const unsigned long WIFI_SILENCIO_MS     = 3000;   // pausa depois de uma falha de conexao
static const unsigned long WIFI_RELOGIN_MS      = 5000;   // intervalo entre tentativas de login enquanto nao ha sessao
static const unsigned long WIFI_POLL_MS         = 1000;   // intervalo entre buscas de comandos

static WiFiClientSecure g_wifiCli;
static HTTPClient g_wifiHttp;
static String g_wifiToken;
static unsigned long g_wifiProximoPoll = 0;
static unsigned long g_wifiProximoLogin = 0;
static unsigned long g_wifiSilencioAte = 0;

static String wifiUrl(const String& rota) { return String(FLOW_BRIDGE_URL) + rota; }

static bool wifiEmSilencio() { return g_wifiSilencioAte != 0 && millis() < g_wifiSilencioAte; }

// Registra o resultado de uma chamada: falha de conexao (codigo <= 0) silencia a rede por um tempo.
static void wifiNotar(int code) {
  g_wifiSilencioAte = (code <= 0) ? millis() + WIFI_SILENCIO_MS : 0;
}

// POST JSON. Devolve o codigo HTTP (ou -1 sem rede/silencio) e, em *resp, o corpo.
static int wifiPost(const String& rota, const char* json, String* resp = nullptr) {
  if (WiFi.status() != WL_CONNECTED || wifiEmSilencio() || !g_wifiHttp.begin(g_wifiCli, wifiUrl(rota))) return -1;
  g_wifiHttp.addHeader("Content-Type", "application/json");
  if (g_wifiToken.length()) g_wifiHttp.addHeader("Authorization", "Bearer " + g_wifiToken);
  int code = g_wifiHttp.POST((uint8_t*)json, strlen(json));
  if (resp && code > 0) *resp = g_wifiHttp.getString();
  g_wifiHttp.end();
  wifiNotar(code);
  return code;
}

void wifiIniciar() {
  WiFi.begin("Wokwi-GUEST", "", 6);                       // canal 6 acelera a conexao no Wokwi
  for (int i = 0; i < 100 && WiFi.status() != WL_CONNECTED; i++) delay(200);
  configTime(0, 0, "pool.ntp.org", "time.google.com");    // horario para validar o prazo do certificado
  for (int i = 0; i < 50 && time(nullptr) < 1700000000; i++) delay(200);
  g_wifiCli.setCACert(FLOW_CA_PEM);                        // so a CA do projeto
  g_wifiHttp.setReuse(true);                               // uma conexao TLS serve varias chamadas (o bridge fala HTTP/1.1)
  g_wifiHttp.setConnectTimeout(WIFI_TIMEOUT_MS);
  g_wifiHttp.setTimeout(WIFI_TIMEOUT_MS);
  Serial.println(WiFi.status() == WL_CONNECTED ? "WiFi conectado" : "WiFi NAO conectou");
  if (time(nullptr) < 1700000000) Serial.println("AVISO: sem horario (NTP); o certificado do bridge nao sera validado");
  Serial.print("Bridge: "); Serial.println(FLOW_BRIDGE_URL);
}

// Login: o corpo e a linha JSON de login; o bridge repassa ao servidor e devolve so um token LOCAL, guardado aqui em memoria.
// O segredo sai direto para a conexao TLS: nunca vai para a serial nem para o log.
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

// Sessao perdida (bridge reiniciado ou token recusado): o bridge nao guarda o segredo, entao o firmware refaz o login.
static void wifiSessaoPerdida() {
  g_wifiToken = "";
  g_wifiProximoLogin = 0;
}

void wifiEnviarEvento(const char* json) {
  if (!g_wifiToken.length()) return;
  int code = wifiPost(String("/v1/dispositivos/") + DEVICE_ID + "/eventos", json);
  if (code == 401) wifiSessaoPerdida();
}

// Chamado a cada volta do loop. Sem sessao, tenta o login de tempos em tempos (o bridge pode subir depois do firmware);
// com sessao, a cada 1 s busca os comandos prontos e entrega cada um ao mesmo parser de sempre (receberComando).
void wifiBuscarComandos() {
  if (!g_wifiToken.length()) {
    if (millis() >= g_wifiProximoLogin) {
      g_wifiProximoLogin = millis() + WIFI_RELOGIN_MS;
      emitirLogin();
    }
    return;
  }
  if (millis() < g_wifiProximoPoll || WiFi.status() != WL_CONNECTED || wifiEmSilencio()) return;
  g_wifiProximoPoll = millis() + WIFI_POLL_MS;
  if (!g_wifiHttp.begin(g_wifiCli, wifiUrl(String("/v1/dispositivos/") + DEVICE_ID + "/comandos"))) return;
  g_wifiHttp.addHeader("Authorization", "Bearer " + g_wifiToken);
  int code = g_wifiHttp.GET();
  String corpo = code == 200 ? g_wifiHttp.getString() : String();
  g_wifiHttp.end();
  wifiNotar(code);
  if (code == 401) { wifiSessaoPerdida(); return; }
  int i = 0;                                              // corpo: {"comandos":["CMD;...","CMD;..."]}
  while ((i = corpo.indexOf("\"CMD;", i)) >= 0) {
    int fim = corpo.indexOf('"', i + 1);
    if (fim < 0) break;
    receberComando(corpo.c_str() + i + 1, (size_t)(fim - i - 1));
    i = fim + 1;
  }
}
