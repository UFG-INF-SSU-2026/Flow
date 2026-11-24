#pragma once
// Push do servidor por SSE (GET /push/wokwi, conexao C com certificado), lido SEM bloquear o loop(): a cada volta le no maximo
// PUSH_LEITURA_MAX bytes, decodifica o enquadramento em chunks, junta linhas de SSE e, quando um evento fecha (linha em branco),
// decifra o envelope, valida a mensagem JSON e atua. Substitui as antigas linhas de comando em texto.
//
// Mensagem de push (texto decifrado): schemaVersion (3), messageId, messageType, userId, episodeId, issuedAt, expiresAt e payload.
// Tipos: queda.confirmacao_solicitada (payload.timeoutMs inteiro, payload.motivo) e queda.confirmacao_encerrada
// (payload.resultado: CONFIRMADO, EXPIRADO ou CANCELADO). Quem atua e o sketch.ino (aoPushSolicitado/aoPushEncerrado); a faixa do
// timeoutMs e conferida la (fora de 0 < t <= BUZZER_MAX_MS responde push.ack REJEITADO).
//
// Descartes antes de agir (com motivo curto no log, SEM o conteudo, que veio da rede): schemaVersion diferente de 3, userId de outra
// conta, forma invalida, messageId fora de [A-Za-z0-9-] com 1 a 39 caracteres, tipo desconhecido, timeoutMs que nao e inteiro,
// resultado invalido, JSON ou envelope invalidos, evento acima de LINHA_SSE_MAX e push ja vencido (expiresAt, so com o relogio
// valido). messageId repetido e descartado sem log. Uma mensagem ruim nunca derruba o canal.
//
// O prazo do buzzer conta a partir da CHEGADA (timeoutMs), nao do relogio; o relogio so serve para descartar o que ja venceu.
// Reconexao: ao cair, espera 1 s e reabre com Last-Event-ID do ultimo evento PROCESSADO; falha de abertura dobra a espera ate 10 s.
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include <ArduinoJson.h>
// (envelope.h, transporte_wifi.h e sessao.h ja foram incluidos antes)

void aoPushSolicitado(const char* messageId, long timeoutMs);   // sketch.ino: liga o buzzer (valida a faixa do prazo)
void aoPushEncerrado(const char* resultado);                    // sketch.ino: desliga o buzzer se esta tocando

#define LINHA_SSE_MAX      2048        // maior linha/evento aceito; acima disso o evento inteiro e descartado (nunca truncado)
#define PUSH_LEITURA_MAX   512         // bytes lidos por volta do loop() (~16 ms medidos no simulador com evento)
#define PUSH_IDS_VISTOS    16          // memoria circular de messageId
static const unsigned long PUSH_ESPERA_INICIAL_MS = 1000;
static const unsigned long PUSH_ESPERA_MAX_MS     = 10000;
static const int           PUSH_SCHEMA            = 3;          // schemaVersion da mensagem (docs/schemas.md, secao 6)

// ---- leitor de SSE (puro: sem rede) -------------------------------------------------------------------------------------------
static char   g_sseLinha[LINHA_SSE_MAX + 1];
static size_t g_sseLen = 0;
static bool   g_sseLinhaGrande = false;       // linha maior que o limite: o evento inteiro e descartado ate a linha em branco
static char   g_sseId[24] = "";
static char   g_sseEvento[24] = "";
static char   g_sseDados[LINHA_SSE_MAX + 1];
static bool   g_sseTemDados = false;
static bool   g_sseDescartar = false;
static char   g_ultimoId[24] = "";            // Last-Event-ID: id do ultimo evento PROCESSADO

// ---- decodificador de chunks (HTTP/1.1 chunked) --------------------------------------------------------------------------------
enum EstadoChunk { CK_TAMANHO, CK_EXT, CK_LF_TAMANHO, CK_DADOS, CK_CR, CK_LF };
static EstadoChunk g_ckEstado = CK_TAMANHO;
static unsigned long g_ckResto = 0;
static unsigned long g_ckTamanho = 0;
static int g_ckDigitos = 0;
static bool g_streamFim = false;              // chunk 0 ou enquadramento invalido: o stream acabou

// ---- reconexao e deduplicacao --------------------------------------------------------------------------------------------------
static bool g_pushAberto2 = false;
static unsigned long g_pushProxAbrir = 0;
static unsigned long g_pushEspera = PUSH_ESPERA_INICIAL_MS;
static char g_idsVistos[PUSH_IDS_VISTOS][40];
static uint8_t g_idsProximo = 0;
static bool g_reabrirJa = false;              // `reautenticar`: reabrir na hora, sem esperar 1 s

static void pushLog(const char* motivo) {     // so o motivo curto, nunca o conteudo da mensagem
  Serial.print("push: "); Serial.println(motivo);
}

static void pushReiniciarLeitor() {
  g_sseLen = 0; g_sseLinhaGrande = false; g_sseId[0] = g_sseEvento[0] = g_sseDados[0] = '\0';
  g_sseTemDados = false; g_sseDescartar = false;
  g_ckEstado = CK_TAMANHO; g_ckResto = 0; g_ckTamanho = 0; g_ckDigitos = 0; g_streamFim = false;
}

// ---- validacao ------------------------------------------------------------------------------------------------------------------
static bool pushMessageIdValido(const char* id) {
  size_t n = strlen(id);
  if (n < 1 || n > 39) return false;            // o buffer buzzerMsgId do sketch tem 40 bytes
  for (size_t i = 0; i < n; i++) {
    char c = id[i];
    if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-')) return false;
  }
  return true;
}

// "AAAA-MM-DDThh:mm:ss.mmmZ" (24 caracteres) -> epoch em segundos (UTC). false se a forma nao bater.
static bool pushIsoParaEpoch(const char* s, long long* epoch) {
  if (strlen(s) != 24 || s[4] != '-' || s[7] != '-' || s[10] != 'T' || s[13] != ':' || s[16] != ':' || s[19] != '.' || s[23] != 'Z') return false;
  int num[6]; const int pos[6] = {0, 5, 8, 11, 14, 17}; const int len[6] = {4, 2, 2, 2, 2, 2};
  for (int k = 0; k < 6; k++) {
    int v = 0;
    for (int i = 0; i < len[k]; i++) { char c = s[pos[k] + i]; if (c < '0' || c > '9') return false; v = v * 10 + (c - '0'); }
    num[k] = v;
  }
  for (int i = 20; i < 23; i++) if (s[i] < '0' || s[i] > '9') return false;
  int a = num[0], m = num[1], d = num[2];
  if (m < 1 || m > 12 || d < 1 || d > 31 || num[3] > 23 || num[4] > 59 || num[5] > 60) return false;
  a -= m <= 2;                                   // dias desde 1970 (algoritmo civil -> dias)
  long long era = (a >= 0 ? a : a - 399) / 400;
  long long ano = a - era * 400;
  long long doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
  long long doe = ano * 365 + ano / 4 - ano / 100 + doy;
  long long dias = era * 146097 + doe - 719468;
  *epoch = dias * 86400 + num[3] * 3600 + num[4] * 60 + num[5];
  return true;
}

static bool pushResultadoValido(const char* r) {
  return strcmp(r, "CONFIRMADO") == 0 || strcmp(r, "EXPIRADO") == 0 || strcmp(r, "CANCELADO") == 0;
}

static bool pushJaVisto(const char* id) {
  for (uint8_t i = 0; i < PUSH_IDS_VISTOS; i++) if (strcmp(g_idsVistos[i], id) == 0) return true;
  return false;
}

static void pushLembrar(const char* id) {
  strncpy(g_idsVistos[g_idsProximo], id, sizeof(g_idsVistos[0]) - 1);
  g_idsVistos[g_idsProximo][sizeof(g_idsVistos[0]) - 1] = '\0';
  g_idsProximo = (g_idsProximo + 1) % PUSH_IDS_VISTOS;
}

// Decifra, valida e atua sobre o `data:` de um evento `push`. Devolve true se a mensagem foi processada (atuou ou era repetida);
// false se foi descartada.
static bool pushProcessar(const char* dados, size_t n) {
  static char texto[ENV_TEXTO_MAX + 1];
  int t = envelopeDecifrar(dados, n, texto, sizeof(texto));
  if (t < 0) { pushLog("envelope invalido"); return false; }
  JsonDocument doc;
  if (deserializeJson(doc, texto, t) != DeserializationError::Ok) { memset(texto, 0, sizeof(texto)); pushLog("json invalido"); return false; }
  memset(texto, 0, sizeof(texto));
  if (!doc.is<JsonObject>()) { pushLog("forma invalida"); return false; }

  if (!doc["schemaVersion"].is<int>() || doc["schemaVersion"].as<int>() != PUSH_SCHEMA) { pushLog("schemaVersion nao suportado"); return false; }
  if (!doc["messageId"].is<const char*>() || !doc["messageType"].is<const char*>() || !doc["episodeId"].is<const char*>() ||
      !doc["issuedAt"].is<const char*>() || !doc["expiresAt"].is<const char*>() || !doc["userId"].is<long>() ||
      !doc["payload"].is<JsonObject>()) { pushLog("campo ausente ou de tipo errado"); return false; }
  const char* id = doc["messageId"];
  const char* tipo = doc["messageType"];
  if (!pushMessageIdValido(id)) { pushLog("messageId invalido"); return false; }
  if (doc["userId"].as<long>() != sessaoUserId()) { pushLog("userId de outra conta"); return false; }

  bool solicitada = strcmp(tipo, "queda.confirmacao_solicitada") == 0;
  bool encerrada = strcmp(tipo, "queda.confirmacao_encerrada") == 0;
  if (!solicitada && !encerrada) { pushLog("tipo desconhecido"); return false; }

  long timeoutMs = 0;
  const char* resultado = nullptr;
  JsonObject payload = doc["payload"].as<JsonObject>();
  if (solicitada) {
    if (!payload["timeoutMs"].is<long>() || !payload["motivo"].is<const char*>()) { pushLog("payload invalido"); return false; }
    timeoutMs = payload["timeoutMs"].as<long>();
  } else {
    if (!payload["resultado"].is<const char*>() || !pushResultadoValido(payload["resultado"].as<const char*>())) { pushLog("resultado invalido"); return false; }
    resultado = payload["resultado"].as<const char*>();
  }

  if (pushJaVisto(id)) return true;               // repetido (replay na reconexao): sem log
  long long vence;
  if (!pushIsoParaEpoch(doc["expiresAt"].as<const char*>(), &vence)) { pushLog("expiresAt invalido"); return false; }
  // Vencido: so se descarta com o relogio valido (sem NTP nao se sabe). So o pedido: o encerramento atrasado ainda silencia.
  if (solicitada && wifiRelogioValido() && vence < wifiAgoraEpoch()) { pushLembrar(id); pushLog("pedido ja vencido"); return false; }

  pushLembrar(id);
  if (solicitada) aoPushSolicitado(id, timeoutMs); else aoPushEncerrado(resultado);
  return true;
}

// Entrega um evento SSE completo (a linha em branco ja chegou).
static void pushEvento() {
  if (g_sseDescartar) { pushLog("evento grande demais descartado"); return; }
  if (strcmp(g_sseEvento, "push") == 0 && g_sseTemDados) {
    pushProcessar(g_sseDados, strlen(g_sseDados));
    strncpy(g_ultimoId, g_sseId, sizeof(g_ultimoId) - 1);           // so depois de processar
    g_ultimoId[sizeof(g_ultimoId) - 1] = '\0';
  } else if (strcmp(g_sseEvento, "reautenticar") == 0) {
    pushLog("reautenticar");
    sessaoRenovar();                                                  // token perto de expirar: renova e reabre o stream
    g_reabrirJa = true;
    g_streamFim = true;                                               // para de ler este stream; pushAvancar fecha e reabre
  }
}

static void sseFimDeLinha() {
  if (g_sseLinhaGrande) { g_sseLinhaGrande = false; g_sseLen = 0; g_sseDescartar = true; return; }
  g_sseLinha[g_sseLen] = '\0';
  size_t n = g_sseLen;
  g_sseLen = 0;
  if (n == 0) {                                                        // linha em branco: fecha o evento
    if (g_sseTemDados || g_sseEvento[0] || g_sseDescartar) pushEvento();
    g_sseId[0] = g_sseEvento[0] = g_sseDados[0] = '\0';
    g_sseTemDados = false; g_sseDescartar = false;
    return;
  }
  if (g_sseLinha[0] == ':') return;                                    // comentario (keepalive)
  char* dois = strchr(g_sseLinha, ':');
  if (!dois) return;
  *dois = '\0';
  const char* valor = dois + 1;
  if (*valor == ' ') valor++;
  if (strcmp(g_sseLinha, "id") == 0) { strncpy(g_sseId, valor, sizeof(g_sseId) - 1); g_sseId[sizeof(g_sseId) - 1] = '\0'; }
  else if (strcmp(g_sseLinha, "event") == 0) { strncpy(g_sseEvento, valor, sizeof(g_sseEvento) - 1); g_sseEvento[sizeof(g_sseEvento) - 1] = '\0'; }
  else if (strcmp(g_sseLinha, "data") == 0) { strncpy(g_sseDados, valor, sizeof(g_sseDados) - 1); g_sseDados[sizeof(g_sseDados) - 1] = '\0'; g_sseTemDados = true; }
}

static void sseByte(char c) {
  if (c == '\r') return;
  if (c == '\n') { sseFimDeLinha(); return; }
  if (g_sseLinhaGrande) return;
  if (g_sseLen >= LINHA_SSE_MAX) { g_sseLinhaGrande = true; return; }   // nunca trunca: marca o evento para descarte
  g_sseLinha[g_sseLen++] = c;
}

// Alimenta o decodificador de chunks com bytes do corpo; o que sai dele vai para o leitor de SSE.
static void pushAlimentar(const uint8_t* b, size_t n) {
  for (size_t i = 0; i < n && !g_streamFim; i++) {
    char c = (char)b[i];
    switch (g_ckEstado) {
      case CK_TAMANHO:
        if (c >= '0' && c <= '9') { g_ckTamanho = g_ckTamanho * 16 + (c - '0'); g_ckDigitos++; }
        else if (c >= 'a' && c <= 'f') { g_ckTamanho = g_ckTamanho * 16 + (c - 'a' + 10); g_ckDigitos++; }
        else if (c >= 'A' && c <= 'F') { g_ckTamanho = g_ckTamanho * 16 + (c - 'A' + 10); g_ckDigitos++; }
        else if (c == ';' && g_ckDigitos > 0) g_ckEstado = CK_EXT;
        else if (c == '\r' && g_ckDigitos > 0) g_ckEstado = CK_LF_TAMANHO;
        else { g_streamFim = true; }                                   // enquadramento invalido
        if (g_ckDigitos > 6) g_streamFim = true;
        break;
      case CK_EXT: if (c == '\r') g_ckEstado = CK_LF_TAMANHO; break;
      case CK_LF_TAMANHO:
        if (c != '\n') { g_streamFim = true; break; }
        if (g_ckTamanho == 0) { g_streamFim = true; break; }           // chunk 0: fim do corpo
        g_ckResto = g_ckTamanho; g_ckEstado = CK_DADOS;
        break;
      case CK_DADOS:
        sseByte(c);
        if (--g_ckResto == 0) g_ckEstado = CK_CR;
        break;
      case CK_CR: if (c != '\r') g_streamFim = true; else g_ckEstado = CK_LF; break;
      case CK_LF:
        if (c != '\n') { g_streamFim = true; break; }
        g_ckEstado = CK_TAMANHO; g_ckTamanho = 0; g_ckDigitos = 0;
        break;
    }
  }
}

// ---- conexao ------------------------------------------------------------------------------------------------------------------
static void pushFechado() {                                            // caiu ou terminou: espera 1 s e reabre com Last-Event-ID
  wifiPushFechar();
  g_pushAberto2 = false;
  g_pushEspera = PUSH_ESPERA_INICIAL_MS;
  g_pushProxAbrir = millis() + PUSH_ESPERA_INICIAL_MS;
}

// A cada volta do loop(), depois de sessaoAvancar() (que nao fez operacao de rede): abre o stream (handshake bloqueante) ou le o
// que chegou (nunca bloqueia, no maximo PUSH_LEITURA_MAX bytes).
static void pushAvancar() {
  if (!sessaoOnline()) {                                               // sem sessao/certificado: o stream nao existe
    if (g_pushAberto2) { wifiPushFechar(); g_pushAberto2 = false; }
    return;
  }
  if (!g_pushAberto2) {
    if ((long)(millis() - g_pushProxAbrir) < 0 || !wifiPronto()) return;
    int code = wifiPushAbrir(sessaoToken(), g_ultimoId[0] ? g_ultimoId : nullptr);
    if (code == 200) { pushReiniciarLeitor(); g_pushAberto2 = true; g_pushEspera = PUSH_ESPERA_INICIAL_MS; sessaoNotarCertificado(true); return; }
    g_pushProxAbrir = millis() + g_pushEspera;
    g_pushEspera = g_pushEspera * 2 > PUSH_ESPERA_MAX_MS ? PUSH_ESPERA_MAX_MS : g_pushEspera * 2;
    if (code == WIFI_ERRO_TLS) sessaoNotarCertificado(false);
    else if (!sessaoTratarRecusaDeCertificado(code, wifiPushErro()) && code == 401) sessaoRenovar();   // token vencido: renova
    return;
  }
  uint8_t buf[PUSH_LEITURA_MAX];
  int n = wifiPushLer(buf, sizeof(buf));
  if (n < 0) { pushFechado(); return; }
  if (n > 0) pushAlimentar(buf, (size_t)n);
  if (g_streamFim) {
    pushFechado();
    if (g_reabrirJa) { g_reabrirJa = false; g_pushProxAbrir = millis(); }
  }
}
