#pragma once
// Sessao do firmware: o DISPOSITIVO e o dono das credenciais. Aqui moram o login, os tokens (so em RAM), a renovacao, o certificado
// de cliente e o envio cifrado dos eventos. O bridge nao guarda nada disso: so repassa.
//
// Ordem do boot (uma operacao BLOQUEANTE por volta do loop(), para botoes, buzzer e imobilidade rodarem entre elas):
//   1. login (POST /auth/login, sem certificado de cliente)
//   2. chave EC e CSR (certificado.h)
//   3. emissao (POST /auth/certificado, ainda sem certificado de cliente) -> A, B e C sao fechadas e reabertas com ele
//   4. online: telemetria/eventos por B (mTLS), push por C (push.h), renovacao dos tokens por B
// Enquanto algo nao esta pronto o firmware NAO envia (falha fechada) e o resto do loop() funciona normalmente.
//
// Recuperacao: refazer o login com a senha cobre token expirado, refresh perdido e sessao perdida. Certificado recusado leva a NOVA
// EMISSAO com o mesmo token (sem renovar o token nem refazer o login): a borda diz `401 certificado_ausente` (a chamada chegou sem
// certificado) ou `403 certificado_incompativel` (certificado de outra conta ou de outro device_id); uma conexao derrubada no
// handshake so conta depois de 3 falhas seguidas (um bridge.crt com nome fora da lista tambem derruba e nao se resolve emitindo
// outro). Os pedidos de certificado esperam cada vez mais (5 s, 10 s... ate 60 s). Nunca se envia sem certificado o que a borda
// exige, e a verificacao do certificado do bridge nunca e relaxada.
//
// Segredo, tokens, chave e corpo de login NUNCA vao para a Serial: os logs trazem so o codigo HTTP.
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include <ArduinoJson.h>
#include "segredos.h"          // FLOW_IDENTIFICADOR, FLOW_SEGREDO
// (envelope.h, transporte_wifi.h e certificado.h ja foram incluidos antes)

extern const char* DEVICE_ID;  // sketch.ino

static const int           SESSAO_SCHEMA_AUTH      = 3;       // schemaVersion de login/refresh (docs/schemas.md, secoes 7 e 8)
static const int           SESSAO_SCHEMA_CERTIFICADO = 1;     // schemaVersion de POST /auth/certificado (docs/schemas.md, secao 11)
static const unsigned long SESSAO_ESPERA_CERT_MIN_MS = 5000;  // espera entre pedidos de certificado: cresce ate o maximo
static const unsigned long SESSAO_ESPERA_CERT_MAX_MS = 60000;
static const unsigned long SESSAO_MARGEM_RENOVACAO_S = 300;   // renova o access token 5 min antes de expirar
static const uint8_t       SESSAO_FALHAS_CERT_MAX  = 3;       // falhas seguidas com certificado antes de pedir um novo
static const unsigned long SESSAO_LOG_MS           = 10000;   // no maximo uma linha de log de falha por intervalo

enum FaseSessao { FASE_SEM_SESSAO, FASE_SEM_CHAVE, FASE_SEM_CERT, FASE_ONLINE };

static FaseSessao g_fase = FASE_SEM_SESSAO;
static char g_access[512] = "";
static char g_refresh[128] = "";
static long g_userId = 0;
static unsigned long g_renovarEm = 0;
static bool g_temRenovacao = false;
static unsigned long g_proxTentativa = 0;
static bool g_certPronto = false;
static char g_certPem[1536] = "";
static uint8_t g_falhasCert = 0;
static unsigned long g_esperaCert = SESSAO_ESPERA_CERT_MIN_MS;
static unsigned long g_ultimoLog = 0;

// Buffers fixos (sem alocacao por chamada): texto claro, envelope enviado e resposta recebida.
static char g_plano[ENV_TEXTO_MAX + 1];
static char g_env[ENV_JSON_MAX];
static char g_resp[ENV_JSON_MAX + 64];

static void sessaoLog(const char* mensagem, int code) {
  if (g_ultimoLog != 0 && millis() - g_ultimoLog < SESSAO_LOG_MS) return;
  g_ultimoLog = millis() ? millis() : 1;
  Serial.print(mensagem);
  if (code != 0) { Serial.print(" (HTTP "); Serial.print(code); Serial.println(")"); } else Serial.println("");
}

static bool sessaoOnline() { return g_fase == FASE_ONLINE && g_access[0] != '\0'; }
static long sessaoUserId() { return g_userId; }
static const char* sessaoToken() { return g_access; }

static void sessaoLimparPlano() { memset(g_plano, 0, sizeof(g_plano)); }

static void sessaoLimparTokens() {
  memset(g_access, 0, sizeof(g_access));
  memset(g_refresh, 0, sizeof(g_refresh));
  g_userId = 0;
  g_temRenovacao = false;
}

// Cifra g_plano (n bytes) e faz o POST; a resposta (envelope) fica em g_resp. Devolve o codigo HTTP ou WIFI_*.
static int sessaoChamar(const char* rota, size_t n, bool comToken) {
  int e = envelopeCifrar(g_plano, n, g_env, sizeof(g_env));
  memset(g_plano, 0, sizeof(g_plano));                          // o texto claro (pode ter a senha) nao fica na RAM
  if (e < 0) return WIFI_SEM_REDE;
  size_t m = 0;
  int code = wifiPost(rota, comToken ? g_access : nullptr, g_env, g_resp, sizeof(g_resp), &m);
  return code;
}

// Decifra a resposta (g_resp) para g_plano e a le como JSON. false se algo falhar. Quem chama deve zerar g_plano (sessaoLimparPlano)
// ao terminar de usar o documento: ele pode conter tokens.
static bool sessaoLerResposta(JsonDocument& doc) {
  int n = envelopeDecifrar(g_resp, strlen(g_resp), g_plano, sizeof(g_plano));
  if (n < 0) return false;
  return deserializeJson(doc, g_plano, n) == DeserializationError::Ok;       // o chamador zera g_plano depois de usar o documento
}

// Guarda access/refresh/user_id da resposta de login ou refresh. false se faltar algum campo (nada e guardado pela metade).
static bool sessaoGuardarTokens(JsonDocument& doc) {
  const char* acesso = doc["access_token"].is<const char*>() ? doc["access_token"].as<const char*>() : nullptr;
  const char* renovacao = doc["refresh_token"].is<const char*>() ? doc["refresh_token"].as<const char*>() : nullptr;
  if (!acesso || !renovacao || strlen(acesso) >= sizeof(g_access) || strlen(renovacao) >= sizeof(g_refresh)) return false;
  long expira = doc["expires_in"].is<long>() ? doc["expires_in"].as<long>() : 0;
  JsonArray ids = doc["user_ids"].as<JsonArray>();
  if (expira <= 0 || ids.isNull() || ids.size() == 0 || !ids[0].is<long>()) return false;
  strcpy(g_access, acesso);
  strcpy(g_refresh, renovacao);
  g_userId = ids[0].as<long>();
  unsigned long antes = expira > (long)SESSAO_MARGEM_RENOVACAO_S ? (unsigned long)expira - SESSAO_MARGEM_RENOVACAO_S : 1;
  g_renovarEm = millis() + antes * 1000UL;
  g_temRenovacao = true;
  return true;
}

// ---- certificado recusado (conexoes B e C) -------------------------------------------------------------------------------------
static void sessaoEsperarProximoCertificado() {
  g_proxTentativa = millis() + g_esperaCert;
  g_esperaCert = g_esperaCert * 2 > SESSAO_ESPERA_CERT_MAX_MS ? SESSAO_ESPERA_CERT_MAX_MS : g_esperaCert * 2;
}

// A borda recusou o certificado, ou o bridge o derrubou 3 vezes: descarta-o e pede outro, com o MESMO token (sem novo login).
// Sem token (ou depois de o refresh falhar) o caminho comeca pelo login. Fecha B e C: nada protegido sai ate o certificado novo.
static void sessaoDescartarCertificado(const char* motivo) {
  g_falhasCert = 0;
  g_certPronto = false;
  g_certPem[0] = '\0';
  wifiSemCertificado();
  g_fase = g_access[0] ? FASE_SEM_CERT : FASE_SEM_SESSAO;
  sessaoEsperarProximoCertificado();
  sessaoLog(motivo, 0);
}

// Resultado de uma chamada ou abertura do push COM certificado: `ok` (2xx) zera a contagem e a espera; um handshake derrubado
// (WIFI_ERRO_TLS) conta, e na 3a falha seguida pede um certificado novo.
static void sessaoNotarCertificado(bool ok) {
  if (ok) { g_falhasCert = 0; g_esperaCert = SESSAO_ESPERA_CERT_MIN_MS; return; }
  if (++g_falhasCert >= SESSAO_FALHAS_CERT_MAX) sessaoDescartarCertificado("handshake com certificado falhou 3 vezes: pedindo um novo");
}

// A borda (repassada pelo bridge) recusou o certificado: `corpo` e o JSON de erro. true se era uma recusa de certificado (e ja a tratou).
static bool sessaoTratarRecusaDeCertificado(int code, const char* corpo) {
  if (code == 401 && strstr(corpo, "certificado_ausente")) { sessaoDescartarCertificado("borda: certificado ausente; emitindo outro"); return true; }
  if (code == 403 && strstr(corpo, "certificado_incompativel")) { sessaoDescartarCertificado("borda: certificado de outra conta ou device; emitindo outro"); return true; }
  return false;
}

// ---- login, renovacao e emissao ---------------------------------------------------------------------------------------------
static bool sessaoLogin() {
  JsonDocument corpo;
  corpo["schemaVersion"] = SESSAO_SCHEMA_AUTH;
  corpo["identificador"] = FLOW_IDENTIFICADOR;
  corpo["segredo"] = FLOW_SEGREDO;
  corpo["fonte"] = "wokwi";
  corpo["device_id"] = DEVICE_ID;
  size_t n = serializeJson(corpo, g_plano, sizeof(g_plano));
  int code = sessaoChamar("/auth/login", n, false);
  if (code != 200) { sessaoLog("login nao aceito", code); return false; }
  JsonDocument resp;
  bool ok = sessaoLerResposta(resp) && sessaoGuardarTokens(resp);
  sessaoLimparPlano();
  if (!ok) { sessaoLog("login: resposta invalida", 0); sessaoLimparTokens(); return false; }
  Serial.println("login aceito");
  return true;
}

// Renova os dois tokens (o refresh e de uso unico). true se renovou; recusado (400/401/403) apaga a sessao para refazer o login;
// falha de rede ou 5xx mantem os tokens e tenta na volta seguinte (respeitando o silencio da rede).
static bool sessaoRenovar() {
  if (!g_temRenovacao) return false;
  JsonDocument corpo;
  corpo["schemaVersion"] = SESSAO_SCHEMA_AUTH;
  corpo["refresh_token"] = g_refresh;
  size_t n = serializeJson(corpo, g_plano, sizeof(g_plano));
  int code = sessaoChamar("/auth/refresh", n, false);
  if (code == WIFI_ERRO_TLS) { sessaoNotarCertificado(false); return false; }
  if (code == 400 || code == 401 || code == 403) {
    sessaoLimparTokens();
    g_fase = FASE_SEM_SESSAO;
    g_proxTentativa = 0;
    sessaoLog("sessao recusada: novo login", code);
    return false;
  }
  if (code != 200) { g_renovarEm = millis() + 1000UL; sessaoLog("renovacao falhou", code); return false; }
  sessaoNotarCertificado(true);
  JsonDocument resp;
  bool ok = sessaoLerResposta(resp) && sessaoGuardarTokens(resp);
  sessaoLimparPlano();
  if (!ok) { sessaoLimparTokens(); g_fase = FASE_SEM_SESSAO; g_proxTentativa = 0; return false; }
  return true;
}

// Pede o certificado com o CSR gerado no boot. Em sucesso guarda o PEM em RAM e passa a usar mTLS (fecha A, abre B e C na volta).
static bool sessaoEmitir() {
  JsonDocument corpo;
  corpo["schemaVersion"] = SESSAO_SCHEMA_CERTIFICADO;
  corpo["csr"] = g_csrPem;
  size_t n = serializeJson(corpo, g_plano, sizeof(g_plano));
  int code = sessaoChamar("/auth/certificado", n, true);
  if (code == 401) { sessaoLimparTokens(); g_fase = FASE_SEM_SESSAO; g_proxTentativa = 0; sessaoLog("emissao: token recusado", code); return false; }
  if (code == 400) g_esperaCert = SESSAO_ESPERA_CERT_MAX_MS;      // csr_invalido e erro de programacao: nao repetir em laco
  if (code != 200) { sessaoLog(code == 400 ? "emissao: CSR recusado pelo servidor" : "emissao do certificado falhou", code); return false; }
  JsonDocument resp;
  bool ok = sessaoLerResposta(resp) && resp["certificado"].is<const char*>() &&
            strlen(resp["certificado"].as<const char*>()) < sizeof(g_certPem);
  if (ok) strcpy(g_certPem, resp["certificado"].as<const char*>());
  sessaoLimparPlano();
  if (!ok) { sessaoLog("emissao: resposta invalida", 0); return false; }
  g_certPronto = true;
  g_falhasCert = 0;
  g_esperaCert = SESSAO_ESPERA_CERT_MIN_MS;
  wifiUsarCertificado(g_certPem, g_chavePem);
  Serial.println("certificado emitido");
  return true;
}

// Avanca a maquina do boot e a renovacao. Chamar a cada volta do loop(). Devolve true se fez uma operacao de rede (bloqueante):
// o loop() pula o push nessa volta, para nunca juntar dois handshakes.
static bool sessaoAvancar() {
  if (!wifiPronto()) return false;
  unsigned long agora = millis();
  switch (g_fase) {
    case FASE_SEM_SESSAO:
      if (agora < g_proxTentativa) return false;
      if (sessaoLogin()) { g_fase = g_certPronto ? FASE_ONLINE : (g_chaveGerada ? FASE_SEM_CERT : FASE_SEM_CHAVE); g_proxTentativa = 0; }
      else g_proxTentativa = agora + WIFI_RELOGIN_MS;                 // so a FALHA espera; o sucesso segue para o proximo passo
      return true;
    case FASE_SEM_CHAVE:
      if (agora < g_proxTentativa) return false;
      if (certificadoGerar(DEVICE_ID)) { g_fase = FASE_SEM_CERT; g_proxTentativa = 0; }
      else g_proxTentativa = agora + WIFI_RELOGIN_MS;
      return true;
    case FASE_SEM_CERT:
      if (agora < g_proxTentativa) return false;
      if (sessaoEmitir()) { g_fase = FASE_ONLINE; g_proxTentativa = 0; }
      else if (g_fase == FASE_SEM_CERT) sessaoEsperarProximoCertificado();            // 503, rede... : espera crescente (um 401 voltou ao login)
      return true;
    case FASE_ONLINE:
      if (g_temRenovacao && (long)(agora - g_renovarEm) >= 0) { sessaoRenovar(); return true; }
      return false;
  }
  return false;
}

// Chamada autenticada por B: 401 -> renova uma vez e repete a MESMA chamada uma vez. O corpo ja esta em g_plano (n bytes) e e
// recifrado a cada tentativa (o nonce nunca se repete).
static int sessaoPostAutenticado(const char* rota, const char* json) {
  size_t n = strlen(json);
  if (n > ENV_TEXTO_MAX) return WIFI_SEM_REDE;
  memcpy(g_plano, json, n + 1);
  int code = sessaoChamar(rota, n, true);
  if (code == 401 && !strstr(g_resp, "certificado_ausente")) {         // token vencido (nao e falta de certificado): renova e repete uma vez
    if (!sessaoRenovar()) return code;
    memcpy(g_plano, json, n + 1);
    code = sessaoChamar(rota, n, true);
  }
  if (sessaoTratarRecusaDeCertificado(code, g_resp)) return code;     // o evento e descartado; o proximo sai com o certificado novo
  if (code == WIFI_ERRO_TLS) sessaoNotarCertificado(false);
  else if (code >= 200 && code < 300) sessaoNotarCertificado(true);
  return code;
}

// Envia um evento ja montado (JSON do firmware). Sem sessao e sem certificado NADA sai (o evento e descartado, sem fila). Os
// retornos de confirmacao (push.ack, confirmacao.resposta) vao para /eventos; todo o resto, para /dados.
static void sessaoEnviar(const char* json, bool retorno) {
  if (!sessaoOnline() || !wifiPronto()) return;                 // sem Wi-Fi ou sem horario (certificado nao valida): nada sai
  int code = sessaoPostAutenticado(retorno ? "/eventos" : "/dados", json);
  if (code <= 0 && code != WIFI_SEM_REDE) sessaoLog("envio nao entregue", 0);
}
