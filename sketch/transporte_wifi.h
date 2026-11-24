#pragma once
// UNICO transporte do firmware: HTTPS direto com o bridge (Bridge/), pelo WiFi do Wokwi. O ESP32 simulado fala TLS 1.2+ com o
// bridge, confere o certificado dele contra a CA do projeto (ca_flow.h, gerado por SensorServer/scripts/gerar_certificado.py)
// e NUNCA desliga essa verificacao (nenhum modo inseguro do cliente TLS e usado). Aqui so ha WiFi, NTP e TLS; o protocolo (envelope, login, tokens,
// push) mora em sessao.h e push.h, que so enxergam as funcoes wifi* abaixo.
//
// O que precisa ser verdade para funcionar (passo a passo em INSTRUCOES.md):
//  - O ESP32 do Wokwi so alcanca o PC pelo gateway de rede do Wokwi (host.wokwi.internal). Em Wokwi para VS Code, rode o
//    wokwigw ANTES da simulacao e mantenha o "[net] gateway" do wokwi.toml. O bridge precisa estar no ar.
//  - O certificado da API do bridge precisa listar o nome da URL (gerar_certificado.py --nome bridge --dns host.wokwi.internal).
//  - O certificado tem prazo: o ESP32 precisa do horario certo (NTP) antes do handshake; sem ele NADA e enviado (e a verificacao
//    nunca e relaxada).
//
// Tres conexoes, cada uma com o seu cliente TLS (o HTTPClient manda a requisicao para a conexao que o cliente ja tem aberta,
// mesmo com outra URL, entao nunca se compartilha cliente entre papeis):
//   A  sem certificado de cliente: login e emissao do certificado (so antes de o certificado existir);
//   B  COM certificado (mTLS): /dados e /eventos (e login, refresh e emissao depois que o certificado existe);
//   C  COM certificado: o push por SSE (GET /push/wokwi), lido sem bloquear o loop().
// O certificado so vale numa conexao NOVA: ao recebe-lo, A, B e C sao fechadas e reabertas ja com ele.
//
// Cada handshake custa ~3 s (sem certificado de cliente) a ~5 s (com) no ESP32 simulado e BLOQUEIA o loop() nesse tempo; uma
// chamada numa conexao ja aberta leva ~0,07 s. O bridge fecha a conexao ociosa por mais de 10 s: a chamada seguinte paga um
// handshake de novo. Tempos medidos no simulador; em hardware real podem ser bem diferentes (nao verificado).
//
// Sem fila de retransmissao: se a chamada falha, o evento e descartado e a leitura seguinte prevalece. Depois de uma falha de
// conexao a rede fica em silencio por alguns segundos, para o laco principal (botoes, buzzer) nao ficar preso.
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <time.h>
#include "ca_flow.h"               // FLOW_CA_PEM: so o certificado PUBLICO da CA (gerado por gerar_certificado.py)

#ifndef FLOW_BRIDGE_URL
#define FLOW_BRIDGE_URL "https://host.wokwi.internal:5443"
#endif

// Garantia em tempo de compilacao: a URL do bridge tem de ser https://. Uma URL http:// nem compila.
constexpr bool flowUrlEhHttps(const char* s) {
  return s[0] == 'h' && s[1] == 't' && s[2] == 't' && s[3] == 'p' && s[4] == 's' && s[5] == ':' && s[6] == '/' && s[7] == '/';
}
static_assert(flowUrlEhHttps(FLOW_BRIDGE_URL), "FLOW_BRIDGE_URL precisa comecar com https:// (toda ponta fala TLS)");

// Codigos devolvidos por wifiPost/wifiPushAbrir alem do HTTP (que e > 0)
constexpr int WIFI_SEM_REDE        = -1;   // sem Wi-Fi, em silencio ou conexao que nao completou: nada foi entregue
constexpr int WIFI_SEM_CERTIFICADO = -2;   // rota que exige certificado e ele ainda nao existe: NADA e enviado (falha fechada)
constexpr int WIFI_ERRO_TLS        = -3;   // handshake COM certificado recusado ou falho

static const unsigned long WIFI_TIMEOUT_MS  = 3000;    // conexao e resposta; curto de proposito (ver acima)
static const unsigned long WIFI_SILENCIO_MS = 3000;    // pausa depois de uma falha de conexao
static const unsigned long WIFI_RELOGIN_MS  = 5000;    // intervalo entre tentativas de login/emissao enquanto nao ha sessao
static const unsigned long WIFI_NTP_MS      = 10000;   // intervalo para pedir o horario de novo enquanto ele nao chega
static const long          WIFI_HORARIO_MIN = 1700000000;   // epoch acima disso = relogio acertado

static WiFiClientSecure g_cliA, g_cliB, g_cliC;
static HTTPClient g_httpA, g_httpB;
static bool g_certConfigurado = false;
static unsigned long g_silencioAte = 0;
static bool g_pushAberto = false;
static char g_pushErro[256] = "";           // corpo do erro da ultima abertura recusada (JSON da borda), p.ex. certificado_ausente
static char g_host[64] = "";
static uint16_t g_porta = 443;

void wifiPushFechar();                                     // definida mais abaixo

static bool wifiEmSilencio() { return g_silencioAte != 0 && (long)(millis() - g_silencioAte) < 0; }
static void wifiNotar(int code) { g_silencioAte = (code <= 0) ? millis() + WIFI_SILENCIO_MS : 0; }

static bool wifiRelogioValido() { return time(nullptr) > WIFI_HORARIO_MIN; }
static long long wifiAgoraEpoch() { return (long long)time(nullptr); }

// As rotas a que a BORDA nao exige certificado de cliente (quem as chama ainda pode nao ter um). Todas as demais so saem por B, com
// certificado: sem ele nada e enviado (falha fechada). A decisao final e da borda; esta lista so evita enviar o que ela recusaria.
static bool wifiRotaLivre(const char* rota) {
  return strcmp(rota, "/auth/login") == 0 || strcmp(rota, "/auth/refresh") == 0 || strcmp(rota, "/auth/certificado") == 0;
}

static void wifiLerUrl() {                                // FLOW_BRIDGE_URL -> host e porta
  const char* p = FLOW_BRIDGE_URL + 8;
  size_t i = 0;
  while (*p && *p != ':' && *p != '/' && i < sizeof(g_host) - 1) g_host[i++] = *p++;
  g_host[i] = '\0';
  if (*p == ':') g_porta = (uint16_t)atoi(p + 1);
}

void wifiIniciar() {
  WiFi.begin("Wokwi-GUEST", "", 6);                       // canal 6 acelera a conexao no Wokwi
  for (int i = 0; i < 100 && WiFi.status() != WL_CONNECTED; i++) delay(200);
  configTime(0, 0, "pool.ntp.org", "time.google.com");    // horario para validar o prazo do certificado
  for (int i = 0; i < 50 && !wifiRelogioValido(); i++) delay(200);
  wifiLerUrl();
  g_cliA.setCACert(FLOW_CA_PEM);                          // so a CA do projeto, nos tres clientes
  g_cliB.setCACert(FLOW_CA_PEM);
  g_cliC.setCACert(FLOW_CA_PEM);
  g_httpA.setReuse(true);                                 // uma conexao TLS serve varias chamadas (o bridge fala HTTP/1.1 persistente)
  g_httpB.setReuse(true);
  g_httpA.setConnectTimeout(WIFI_TIMEOUT_MS); g_httpA.setTimeout(WIFI_TIMEOUT_MS);
  g_httpB.setConnectTimeout(WIFI_TIMEOUT_MS); g_httpB.setTimeout(WIFI_TIMEOUT_MS);
  Serial.println(WiFi.status() == WL_CONNECTED ? "WiFi conectado" : "WiFi NAO conectou");
  if (!wifiRelogioValido()) Serial.println("AVISO: sem horario (NTP); nada sera enviado ate o horario chegar");
  Serial.print("Bridge: "); Serial.println(FLOW_BRIDGE_URL);
}

// Wi-Fi no ar E horario acertado. Sem horario o certificado do bridge nao valida: espera e pede de novo, nunca relaxa a verificacao.
bool wifiPronto() {
  if (WiFi.status() != WL_CONNECTED) return false;
  if (wifiRelogioValido()) return true;
  static unsigned long ultimo = 0;
  if (millis() - ultimo >= WIFI_NTP_MS) { ultimo = millis(); configTime(0, 0, "pool.ntp.org", "time.google.com"); }
  return false;
}

// Passa a usar o certificado e a chave do dispositivo (PEM em buffers que continuam validos enquanto os clientes existirem).
// Fecha A, B e C: o certificado so vale em conexao NOVA.
void wifiUsarCertificado(const char* certPem, const char* chavePem) {
  wifiPushFechar();
  g_httpA.end(); g_httpB.end();
  g_cliA.stop(); g_cliB.stop(); g_cliC.stop();
  g_cliB.setCertificate(certPem); g_cliB.setPrivateKey(chavePem);
  g_cliC.setCertificate(certPem); g_cliC.setPrivateKey(chavePem);
  g_certConfigurado = true;
}

// Certificado invalido ou expirado: fecha B e C e volta a exigir um novo (nada protegido sai antes disso).
void wifiSemCertificado() {
  wifiPushFechar();
  g_httpB.end();
  g_cliB.stop(); g_cliC.stop();
  g_certConfigurado = false;
}

// POST de `corpo` (JSON ja cifrado) em `rota`. Login e emissao usam A (ou B, se o certificado ja existe); as demais rotas SO saem
// por B. `bearer` e o access token (ou nullptr). Devolve o codigo HTTP (ou WIFI_*) e, em resp/*n, o corpo da resposta.
int wifiPost(const char* rota, const char* bearer, const char* corpo, char* resp, size_t cap, size_t* n) {
  *n = 0;
  if (resp && cap) resp[0] = '\0';
  bool livre = wifiRotaLivre(rota);
  if (!livre && !g_certConfigurado) return WIFI_SEM_CERTIFICADO;
  if (WiFi.status() != WL_CONNECTED || wifiEmSilencio()) return WIFI_SEM_REDE;
  bool usaB = g_certConfigurado;
  HTTPClient& http = usaB ? g_httpB : g_httpA;
  WiFiClientSecure& cli = usaB ? g_cliB : g_cliA;
  if (!http.begin(cli, String(FLOW_BRIDGE_URL) + rota)) return WIFI_SEM_REDE;
  http.addHeader("Content-Type", "application/json");
  if (bearer && bearer[0]) http.addHeader("Authorization", String("Bearer ") + bearer);
  int code = http.POST((uint8_t*)corpo, strlen(corpo));
  if (code > 0 && resp) {
    String s = http.getString();
    if (s.length() < cap) { memcpy(resp, s.c_str(), s.length() + 1); *n = s.length(); }
  }
  http.end();
  if (code <= 0 && usaB) {                                // conexao com certificado que nao completou
    char erro[64];
    if (cli.lastError(erro, sizeof(erro)) != 0) code = WIFI_ERRO_TLS;
  }
  wifiNotar(code);
  return code;
}

// ---- Push por SSE (conexao C) ---------------------------------------------------------------------------------------------
static bool wifiLerLinha(WiFiClientSecure& c, char* buf, size_t cap, unsigned long limiteMs) {
  size_t i = 0;
  while (millis() < limiteMs) {
    if (!c.connected() && !c.available()) return false;
    if (!c.available()) { delay(2); continue; }
    char ch = (char)c.read();
    if (ch == '\n') { buf[i] = '\0'; return true; }
    if (ch != '\r' && i < cap - 1) buf[i++] = ch;
  }
  return false;
}

static bool wifiComecaCom(const char* s, const char* prefixo) {       // sem diferenciar maiusculas
  for (; *prefixo; s++, prefixo++) if (tolower((unsigned char)*s) != tolower((unsigned char)*prefixo)) return false;
  return true;
}

// Texto do erro (JSON) da ultima abertura que a borda recusou; vazio se nao houve corpo.
const char* wifiPushErro();

// Abre a conexao C (handshake COM certificado, ~5 s bloqueantes) e manda GET /push/wokwi. Devolve 200 com o stream aberto (corpo em
// chunks), o codigo HTTP se o bridge recusou (stream fechado), ou WIFI_*.
int wifiPushAbrir(const char* bearer, const char* ultimoId) {
  wifiPushFechar();
  if (!g_certConfigurado) return WIFI_SEM_CERTIFICADO;
  if (WiFi.status() != WL_CONNECTED || wifiEmSilencio()) return WIFI_SEM_REDE;
  if (!g_cliC.connect(g_host, g_porta, (int32_t)WIFI_TIMEOUT_MS)) {
    char erro[64];
    int r = g_cliC.lastError(erro, sizeof(erro)) != 0 ? WIFI_ERRO_TLS : WIFI_SEM_REDE;
    g_cliC.stop();
    wifiNotar(r);
    return r;
  }
  String req = String("GET /push/wokwi HTTP/1.1\r\nHost: ") + g_host + "\r\nAuthorization: Bearer " + bearer +
               "\r\nAccept: text/event-stream\r\nCache-Control: no-cache\r\n";
  if (ultimoId && ultimoId[0]) req += String("Last-Event-ID: ") + ultimoId + "\r\n";
  req += "\r\n";
  g_cliC.print(req);
  char linha[160];
  unsigned long limite = millis() + WIFI_TIMEOUT_MS;
  if (!wifiLerLinha(g_cliC, linha, sizeof(linha), limite) || strncmp(linha, "HTTP/1.1 ", 9) != 0) { g_cliC.stop(); return WIFI_SEM_REDE; }
  int code = atoi(linha + 9);
  bool chunked = false;
  size_t tamanho = 0;
  g_pushErro[0] = '\0';
  while (true) {                                          // cabecalhos ate a linha em branco
    if (!wifiLerLinha(g_cliC, linha, sizeof(linha), limite)) { g_cliC.stop(); return WIFI_SEM_REDE; }
    if (linha[0] == '\0') break;
    if (wifiComecaCom(linha, "transfer-encoding:") && strstr(linha, "chunked")) chunked = true;
    if (wifiComecaCom(linha, "content-length:")) tamanho = (size_t)atoi(strchr(linha, ':') + 1);
  }
  if (code != 200) {                                      // recusa da borda (repassada pelo bridge): guarda o JSON de erro, que diz o motivo
    size_t i = 0;
    while (i < tamanho && i < sizeof(g_pushErro) - 1 && millis() < limite) {
      if (g_cliC.available()) g_pushErro[i++] = (char)g_cliC.read(); else delay(2);
    }
    g_pushErro[i] = '\0';
    g_cliC.stop();
    wifiNotar(code);
    return code;
  }
  if (!chunked) { g_cliC.stop(); return WIFI_SEM_REDE; }
  g_pushAberto = true;
  wifiNotar(200);
  return 200;
}

const char* wifiPushErro() { return g_pushErro; }

void wifiPushFechar() {
  g_pushAberto = false;
  g_cliC.stop();
}

// Le do stream SEM bloquear, ate `max` bytes do CORPO (ainda em chunks; push.h os decodifica). 0 = nada novo; < 0 = o stream acabou
// ou o soquete caiu.
int wifiPushLer(uint8_t* buf, size_t max) {
  if (!g_pushAberto) return -1;
  int disponivel = g_cliC.available();
  if (disponivel <= 0) {
    if (!g_cliC.connected()) { g_pushAberto = false; return -1; }
    return 0;
  }
  if ((size_t)disponivel > max) disponivel = (int)max;
  int lidos = g_cliC.read(buf, disponivel);
  return lidos > 0 ? lidos : 0;
}
