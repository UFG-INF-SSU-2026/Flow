#pragma once
// Duble de ../transporte_wifi.h para o teste no PC (o de verdade precisa do WiFi/TLS do ESP32). Mantem as MESMAS funcoes wifi* que
// sessao.h e push.h usam, e no lugar da rede poe um SERVIDOR FALSO programavel pelo teste:
//   g_servidor     recebe (rota, bearer, texto claro do envelope) e devolve o codigo HTTP e o texto claro da resposta;
//                  quando 200, a resposta volta CIFRADA com o envelope real do firmware (envelope.h)
//   g_chamadas     tudo que o firmware mandou (rota, bearer, texto claro, se havia certificado configurado)
//   g_rede_eventos so o texto claro de POST /dados e /eventos, em ordem (o que o servidor veria)
//   g_sse          bytes que o "bridge" entrega no stream do push (o teste os monta em chunks)
// A Serial e separada: o teste confere que nela nao passa evento, push, token, chave nem segredo.
#include "Arduino.h"
#include <functional>
#include <string>
#include <vector>

constexpr int WIFI_SEM_REDE = -1, WIFI_SEM_CERTIFICADO = -2, WIFI_ERRO_TLS = -3;
static const unsigned long WIFI_RELOGIN_MS = 5000;

struct Chamada { std::string rota, bearer, plano; bool comCertificado; };
static std::vector<Chamada> g_chamadas;
static std::string g_rede_eventos;
static std::function<int(const std::string&, const std::string&, const std::string&, std::string&)> g_servidor;
static std::function<int(const std::string&)> g_forcar_codigo;      // se devolver != 0, esse codigo (WIFI_* ou HTTP) e o resultado

static bool g_wifi_ok = true, g_relogio_ok = true, g_cert_cfg = false;
static long long g_agora_epoch = 1800000000;                          // 2027-01-15, so um instante qualquer valido
static int g_wifi_iniciado = 0, g_certificados_usados = 0, g_sem_certificado = 0;
static std::string g_cert_usado, g_chave_usada;

static std::string g_sse;                                             // bytes ainda nao lidos do stream
static bool g_sse_aberto = false, g_sse_cai = false;
static int g_push_codigo = 200, g_push_aberturas = 0;
static std::string g_push_bearer, g_push_ultimo_id, g_push_erro;       // g_push_erro: corpo do erro que a borda devolve ao abrir o push

void wifiPushFechar() { g_sse_aberto = false; }
const char* wifiPushErro() { return g_push_erro.c_str(); }
void wifiIniciar() { g_wifi_iniciado++; }
bool wifiPronto() { return g_wifi_ok && g_relogio_ok; }
bool wifiRelogioValido() { return g_relogio_ok; }
long long wifiAgoraEpoch() { return g_agora_epoch; }

void wifiUsarCertificado(const char* cert, const char* chave) {
  wifiPushFechar(); g_cert_cfg = true; g_certificados_usados++; g_cert_usado = cert; g_chave_usada = chave;
}
void wifiSemCertificado() { wifiPushFechar(); g_cert_cfg = false; g_sem_certificado++; }

static bool wifiRotaLivre(const std::string& r) { return r == "/auth/login" || r == "/auth/refresh" || r == "/auth/certificado"; }

int wifiPost(const char* rota, const char* bearer, const char* corpo, char* resp, size_t cap, size_t* n) {
  *n = 0; if (cap) resp[0] = '\0';
  std::string r = rota;
  if (!wifiRotaLivre(r) && !g_cert_cfg) return WIFI_SEM_CERTIFICADO;            // falha fechada: nada e enviado
  if (!g_wifi_ok) return WIFI_SEM_REDE;
  if (g_forcar_codigo) { int c = g_forcar_codigo(r); if (c != 0) return c; }
  static char plano[ENV_TEXTO_MAX + 1];
  int t = envelopeDecifrar(corpo, strlen(corpo), plano, sizeof(plano));
  if (t < 0) return 400;                                                         // o servidor falso nao entende: envelope invalido
  g_chamadas.push_back({r, bearer ? bearer : "", plano, g_cert_cfg});
  if (r == "/dados" || r == "/eventos") { g_rede_eventos += plano; g_rede_eventos += "\n"; }
  std::string saida;
  int code = g_servidor ? g_servidor(r, bearer ? bearer : "", plano, saida) : 200;
  if (code == 200) {
    int e = envelopeCifrar(saida.c_str(), saida.size(), resp, cap);
    if (e > 0) *n = e;
  } else {
    snprintf(resp, cap, "%s", saida.c_str());                                    // erro: JSON simples (do bridge ou da borda)
    *n = strlen(resp);
  }
  return code;
}

int wifiPushAbrir(const char* bearer, const char* ultimoId) {
  wifiPushFechar();
  g_push_aberturas++; g_push_bearer = bearer ? bearer : ""; g_push_ultimo_id = ultimoId ? ultimoId : "";
  if (!g_cert_cfg) return WIFI_SEM_CERTIFICADO;
  if (g_push_codigo != 200) return g_push_codigo;
  g_sse_aberto = true; g_sse_cai = false;
  return 200;
}

int wifiPushLer(uint8_t* buf, size_t max) {
  if (!g_sse_aberto) return -1;
  if (g_sse.empty()) { if (g_sse_cai) { g_sse_aberto = false; return -1; } return 0; }
  size_t k = g_sse.size() < max ? g_sse.size() : max;
  memcpy(buf, g_sse.data(), k); g_sse.erase(0, k);
  return (int)k;
}
