#include "Arduino.h"
#include "sketch_host.cpp"   // inclui os headers do firmware com o transporte_wifi.h DESTA pasta (duble com servidor falso)
#include <iostream>
#include <fstream>
#include <ctime>
static std::string pastaVetores() { const char* d = getenv("FLOW_VETORES_DIR"); return d ? d : "/tmp"; }
static int fails = 0;
#define CHECK(c, m) do { if (c) std::cout << "PASS " << m << "\n"; else { std::cout << "FAIL " << m << "\n"; fails++; } } while (0)

static bool has(const char* t) { return g_rede_eventos.find(t) != std::string::npos; }
static void avanca(unsigned long ms) { for (unsigned long i = 0; i < ms; i += 10) { g_millis += 10; loop(); } }
static std::vector<unsigned long> sequencias() {
  std::vector<unsigned long> r; const std::string chave = "\"sequence\":"; size_t pos = 0;
  while ((pos = g_rede_eventos.find(chave, pos)) != std::string::npos) { pos += chave.size(); r.push_back(strtoul(g_rede_eventos.c_str() + pos, nullptr, 10)); }
  return r;
}
static int contaRota(const char* rota) { int n = 0; for (auto& c : g_chamadas) if (c.rota == rota) n++; return n; }

// ---- servidor falso ----
static int g_logins = 0, g_emissoes = 0, g_refreshes = 0;
static int g_login_codigo = 200, g_emissao_codigo = 200, g_refresh_codigo = 200, g_dados_codigo = 200;
static int g_expira = 3600;
static std::string tokens() {
  return "{\"access_token\":\"acc-" + std::to_string(g_logins + g_refreshes) + "\",\"refresh_token\":\"ref-" + std::to_string(g_logins + g_refreshes) +
         "\",\"expires_in\":" + std::to_string(g_expira) + ",\"papel\":\"dispositivo\",\"user_ids\":[7]}";
}
static void servidorPadrao() {
  g_servidor = [](const std::string& rota, const std::string& bearer, const std::string& plano, std::string& saida) -> int {
    if (rota == "/auth/login") { if (g_login_codigo != 200) { saida = "{\"status\":\"erro\"}"; return g_login_codigo; } g_logins++; saida = tokens(); return 200; }
    if (rota == "/auth/certificado") { if (g_emissao_codigo != 200) { saida = "{\"status\":\"erro\"}"; return g_emissao_codigo; } g_emissoes++;
      saida = "{\"certificado\":\"-----BEGIN CERTIFICATE-----\\nFAKE" + std::to_string(g_emissoes) + "\\n-----END CERTIFICATE-----\\n\"}"; return 200; }
    if (rota == "/auth/refresh") { if (g_refresh_codigo != 200) { saida = "{\"status\":\"erro\"}"; return g_refresh_codigo; } g_refreshes++; saida = tokens(); return 200; }
    if (rota == "/dados" || rota == "/eventos") { if (g_dados_codigo != 200) { saida = "{\"status\":\"erro\"}"; return g_dados_codigo; } saida = "{\"status\":\"ok\"}"; return 200; }
    saida = "{}"; return 404;
  };
}
static void zeraRede() { g_rede_eventos.clear(); g_chamadas.clear(); }

// ---- push: monta eventos SSE em chunks, cifrados com o envelope do firmware ----
static int g_sse_id = 0;
static std::string chunk(const std::string& s) { char h[16]; snprintf(h, sizeof(h), "%zx", s.size()); return std::string(h) + "\r\n" + s + "\r\n"; }
static std::string iso(long long epoch) { time_t t = (time_t)epoch; struct tm g; gmtime_r(&t, &g); char b[40]; strftime(b, sizeof(b), "%Y-%m-%dT%H:%M:%S.000Z", &g); return b; }
static std::string cifrar(const std::string& plano) { static char e[ENV_JSON_MAX]; int n = envelopeCifrar(plano.c_str(), plano.size(), e, sizeof(e)); return n > 0 ? std::string(e, n) : std::string(); }
static std::string msg(const std::string& id, const std::string& tipo, const std::string& payload, const std::string& user = "7", const std::string& schema = "3",
                       long long expiraEm = 60) {
  return "{\"schemaVersion\":" + schema + ",\"messageId\":\"" + id + "\",\"messageType\":\"" + tipo + "\",\"userId\":" + user + ",\"episodeId\":\"ep-1\","
         "\"issuedAt\":\"" + iso(g_agora_epoch) + "\",\"expiresAt\":\"" + iso(g_agora_epoch + expiraEm) + "\",\"payload\":" + payload + "}";
}
static std::string solicitada(const std::string& id, const std::string& timeout) { return msg(id, "queda.confirmacao_solicitada", "{\"timeoutMs\":" + timeout + ",\"motivo\":\"queda\"}"); }
static std::string encerrada(const std::string& id, const std::string& res) { return msg(id, "queda.confirmacao_encerrada", "{\"resultado\":\"" + res + "\"}"); }
static std::string eventoSse(const std::string& dados, const std::string& evento = "push") {
  return "id: " + std::to_string(++g_sse_id) + "\nevent: " + evento + "\ndata: " + dados + "\n\n";
}
static void pushBruto(const std::string& dados) { g_sse += chunk(eventoSse(dados)); }
static void push(const std::string& plano) { pushBruto(cifrar(plano)); }
static void pushSolicitado(const std::string& id, long ms) { push(solicitada(id, std::to_string(ms))); }
static void pushEncerrado(const std::string& id, const std::string& res) { push(encerrada(id, res)); }
static void bridge_off(const char* res) { pushEncerrado(std::string("off-") + std::to_string(++g_sse_id), res); }

static bool online() { return sessaoOnline(); }
static void reinicia() {                                    // estado limpo do servidor falso (a sessao do firmware segue)
  zeraRede(); g_sse.clear();
}

int main() {
  g_pin[PIN_BTN_CONFIRM] = HIGH; g_pin[PIN_BTN_REARM] = HIGH;
  servidorPadrao();
  setup();
  CHECK(Wire.accelCfg == 0x10, "setup escreve ACCEL_CONFIG=0x10 (+-8g)");
  CHECK(g_wifi_iniciado == 1, "setup inicia o WiFi/HTTPS uma vez");
  CHECK(g_aesPronta, "setup carrega a chave AES (32 bytes)");
  CHECK(g_chamadas.empty(), "o setup nao faz login: ele acontece em segundo plano, no loop");

  // ================= BOOT: login -> chave/CSR -> emissao -> online, uma operacao por volta =================
  avanca(10);
  CHECK(contaRota("/auth/login") == 1 && !g_chamadas[0].comCertificado, "1a volta: login, sem certificado de cliente");
  { const std::string& p = g_chamadas[0].plano;
    CHECK(p.find("\"identificador\":\"conta-teste\"") != std::string::npos && p.find("\"segredo\":\"segredo-teste\"") != std::string::npos &&
          p.find("\"device_id\":\"esp32-decisao-imobilidade-01\"") != std::string::npos && p.find("\"fonte\":\"wokwi\"") != std::string::npos &&
          p.find("\"schemaVersion\":3") != std::string::npos, "login leva identificador, segredo, device_id, fonte e schemaVersion 3"); }
  CHECK(!g_chaveGerada && contaRota("/auth/certificado") == 0, "o login nao e seguido, na mesma volta, da chave nem da emissao");
  avanca(10);
  CHECK(g_chaveGerada && contaRota("/auth/certificado") == 0, "2a volta: gera a chave EC e o CSR");
  CHECK(strstr(g_csrPem, "BEGIN CERTIFICATE REQUEST") && strstr(g_chavePem, "BEGIN EC PRIVATE KEY"), "CSR e chave em PEM");
  { std::ofstream(pastaVetores() + "/flow_csr.pem") << g_csrPem; }
  avanca(10);
  CHECK(contaRota("/auth/certificado") == 1 && !g_chamadas.back().comCertificado && g_chamadas.back().bearer == "acc-1", "3a volta: emissao, com o access token e ainda sem certificado de cliente");
  CHECK(g_chamadas.back().plano.find("BEGIN CERTIFICATE REQUEST") != std::string::npos && g_chamadas.back().plano.find("PRIVATE KEY") == std::string::npos, "a emissao leva so o CSR (nunca a chave privada)");
  CHECK(g_cert_cfg && g_certificados_usados == 1 && g_cert_usado.find("FAKE1") != std::string::npos, "recebido o certificado, o transporte passa a usar mTLS (A, B e C reabertas)");
  CHECK(online() && sessaoUserId() == 7, "firmware online com o user_id da sessao");
  CHECK(g_chave_usada == g_chavePem, "o transporte recebeu a chave gerada no boot");
  for (auto& c : g_chamadas) CHECK(c.plano.find("PRIVATE KEY") == std::string::npos, "nenhuma chamada leva a chave privada");
  // Nenhuma rota protegida saiu antes do certificado
  { bool ok = true; bool visto = false; for (auto& c : g_chamadas) { if (c.rota == "/auth/certificado") visto = true; if ((c.rota == "/dados" || c.rota == "/eventos") && !visto) ok = false; }
    CHECK(ok, "nada de /dados nem /eventos antes da emissao do certificado"); }
  avanca(10);
  CHECK(g_push_aberturas == 1 && g_push_bearer == "acc-1" && g_push_ultimo_id.empty(), "depois do certificado, a volta seguinte abre o push (sem Last-Event-ID na 1a vez)");
  g_rede_eventos.clear();
  // leitura: 1 g no eixo Z em +-8g = 4096 LSB -> 9.81 m/s2
  Wire.az = 4096; avanca(1100);
  CHECK(has("\"eventType\":\"imobilidade.leitura\""), "emite imobilidade.leitura");
  CHECK(has("\"value\":9.81") && has("\"valueMax\":9.81"), "value e valueMax corretos com divisor 4096");
  CHECK(has("\"schemaVersion\":3"), "schemaVersion 3");
  CHECK(has("\"user_id\":7") && has("\"source\":\"wokwi\""), "o firmware carimba user_id (da sessao) e source=wokwi (o bridge nao carimba mais)");
  { bool todasComCert = true; for (auto& c : g_chamadas) if ((c.rota == "/dados" || c.rota == "/eventos" || c.rota == "/auth/refresh") && !c.comCertificado) todasComCert = false;
    CHECK(todasComCert, "toda chamada de rota protegida saiu com certificado configurado"); }
  // pico entre ciclos
  g_rede_eventos.clear(); Wire.az = 0; avanca(500); Wire.az = 16384; avanca(100); Wire.az = 0; avanca(500);
  CHECK(has("\"valueMax\":39.2") || has("\"valueMax\":39.3") || has("\"valueMax\":39.22"), "valueMax captura o pico (4 g) amostrado a cada 50 ms");

  // ================= PUSH: pedido de confirmacao, botao, timeout, encerramento =================
  g_rede_eventos.clear();
  pushSolicitado("msg-123", 5000); avanca(30);
  CHECK(g_tone_pin == PIN_BUZZER && g_tone_freq == BUZZER_FREQ_HZ, "push solicitado liga tone no pino do buzzer");
  CHECK(has("\"eventType\":\"push.ack\"") && has("\"state\":\"ATIVADO\"") && has("\"messageId\":\"msg-123\""), "emite push.ack ATIVADO com messageId");
  CHECK(g_chamadas.back().rota == "/eventos", "push.ack vai para /eventos");
  g_rede_eventos.clear();
  g_pin[PIN_BTN_CONFIRM] = LOW; avanca(120); g_pin[PIN_BTN_CONFIRM] = HIGH; avanca(60);
  CHECK(g_tone_pin == -1, "botao Estou bem desliga o buzzer");
  CHECK(has("\"eventType\":\"confirmacao.resposta\"") && has("\"state\":\"ESTOU_BEM\"") && has("msg-123"), "emite confirmacao.resposta ESTOU_BEM");
  g_rede_eventos.clear();
  pushSolicitado("msg-456", 1500); avanca(30);
  CHECK(g_tone_pin == PIN_BUZZER, "segundo pedido liga");
  avanca(1400); CHECK(g_tone_pin == PIN_BUZZER, "ainda toca antes do timeout");
  avanca(300);
  CHECK(g_tone_pin == -1 && has("\"state\":\"SEM_RESPOSTA\"") && has("msg-456"), "timeout desliga e emite SEM_RESPOSTA");
  g_rede_eventos.clear();
  pushSolicitado("msg-789", 5000); avanca(30);
  bridge_off("CONFIRMADO"); avanca(30);
  CHECK(g_tone_pin == -1, "encerramento do servidor desliga");
  CHECK(!has("confirmacao.resposta"), "encerramento do servidor nao emite evento de resposta");
  g_rede_eventos.clear();
  pushSolicitado("msg-x", 999999); avanca(30);
  CHECK(g_tone_pin == -1 && has("\"state\":\"REJEITADO\""), "timeout acima do teto -> REJEITADO e buzzer desligado");
  g_rede_eventos.clear();
  g_pin[PIN_BTN_CONFIRM] = LOW; avanca(120); g_pin[PIN_BTN_CONFIRM] = HIGH; avanca(60);
  CHECK(!has("confirmacao.resposta"), "clique sem buzzer ativo nao emite resposta");
  // Idempotencia
  g_rede_eventos.clear();
  pushSolicitado("msg-dup", 1500); avanca(30);
  CHECK(g_tone_pin == PIN_BUZZER && has("\"messageId\":\"msg-dup\""), "pedido de teste de idempotencia liga");
  avanca(900); g_rede_eventos.clear();
  push(solicitada("msg-dup", "1500")); avanca(30);
  CHECK(!has("push.ack"), "mensagem repetida (mesmo messageId) nao reemite push.ack");
  avanca(700);
  CHECK(g_tone_pin == -1 && has("\"state\":\"SEM_RESPOSTA\"") && has("msg-dup"), "mensagem repetida nao reinicia o tempo");
  g_rede_eventos.clear();
  pushSolicitado("msg-A", 5000); avanca(30); g_rede_eventos.clear();
  pushSolicitado("msg-B", 5000); avanca(30);
  CHECK(g_tone_pin == PIN_BUZZER && has("\"state\":\"ATIVADO\"") && has("\"messageId\":\"msg-B\""), "id diferente substitui: push.ack ATIVADO para o novo");
  CHECK(!has("SEM_RESPOSTA") && !has("msg-A"), "substituicao nao emite SEM_RESPOSTA para o id anterior");
  g_rede_eventos.clear();
  g_pin[PIN_BTN_CONFIRM] = LOW; avanca(120); g_pin[PIN_BTN_CONFIRM] = HIGH; avanca(60);
  CHECK(g_tone_pin == -1 && has("\"state\":\"ESTOU_BEM\"") && has("msg-B") && !has("msg-A"), "resposta depois da substituicao usa o id novo");
  g_rede_eventos.clear();
  bridge_off("CANCELADO"); avanca(30);
  CHECK(g_tone_pin == -1 && !has("push.ack") && !has("confirmacao.resposta"), "encerramento com o buzzer desligado nao emite evento de retorno");
  // sequence unico e crescente
  g_rede_eventos.clear(); avanca(1100);
  pushSolicitado("msg-seq", 5000); avanca(30);
  g_pin[PIN_BTN_CONFIRM] = LOW; avanca(120); g_pin[PIN_BTN_CONFIRM] = HIGH; avanca(60); avanca(1100);
  { std::vector<unsigned long> q = sequencias(); bool cresce = q.size() >= 4;
    for (size_t i = 1; i < q.size(); i++) if (q[i] <= q[i - 1]) cresce = false;
    CHECK(has("imobilidade.leitura") && has("push.ack") && has("confirmacao.resposta"), "o trecho de sequence tem leitura, push.ack e confirmacao.resposta");
    CHECK(cresce, "sequence estritamente crescente entre imobilidade.leitura e eventos de retorno"); }
  g_rede_eventos.clear(); Wire.az = 32767; Wire.ax = 32767; Wire.ay = 32767; avanca(1100);
  CHECK(!has("LEITURA_FORA_DE_FAIXA"), "~136 m/s2 (fundo de escala +-8g) < teto 160: nao e fora de faixa");
  Wire.ax = 0; Wire.ay = 0; Wire.az = 4096;

  // ================= PUSH: mensagens invalidas nunca ligam o buzzer nem derrubam o canal =================
  { struct Caso { const char* nome; std::string plano; };
    std::vector<Caso> ruins = {
      {"schemaVersion 2", msg("bad-1", "queda.confirmacao_solicitada", "{\"timeoutMs\":5000,\"motivo\":\"q\"}", "7", "2")},
      {"schemaVersion texto", msg("bad-2", "queda.confirmacao_solicitada", "{\"timeoutMs\":5000,\"motivo\":\"q\"}", "7", "\"3\"")},
      {"userId de outra conta", msg("bad-3", "queda.confirmacao_solicitada", "{\"timeoutMs\":5000,\"motivo\":\"q\"}", "8")},
      {"userId texto", msg("bad-4", "queda.confirmacao_solicitada", "{\"timeoutMs\":5000,\"motivo\":\"q\"}", "\"7\"")},
      {"messageId com caractere invalido", msg("bad;5", "queda.confirmacao_solicitada", "{\"timeoutMs\":5000,\"motivo\":\"q\"}")},
      {"messageId vazio", msg("", "queda.confirmacao_solicitada", "{\"timeoutMs\":5000,\"motivo\":\"q\"}")},
      {"messageId com 40 caracteres", msg(std::string(40, 'a'), "queda.confirmacao_solicitada", "{\"timeoutMs\":5000,\"motivo\":\"q\"}")},
      {"tipo desconhecido", msg("bad-8", "queda.outra_coisa", "{\"timeoutMs\":5000,\"motivo\":\"q\"}")},
      {"timeoutMs decimal", solicitada("bad-9", "5000.5")},
      {"timeoutMs texto", solicitada("bad-10", "\"5000\"")},
      {"timeoutMs booleano", solicitada("bad-11", "true")},
      {"timeoutMs ausente", msg("bad-12", "queda.confirmacao_solicitada", "{\"motivo\":\"q\"}")},
      {"motivo ausente", msg("bad-13", "queda.confirmacao_solicitada", "{\"timeoutMs\":5000}")},
      {"payload nao e objeto", msg("bad-14", "queda.confirmacao_solicitada", "[1]")},
      {"pedido ja vencido", msg("bad-15", "queda.confirmacao_solicitada", "{\"timeoutMs\":5000,\"motivo\":\"q\"}", "7", "3", -5)},
      {"expiresAt malformado", "{\"schemaVersion\":3,\"messageId\":\"bad-16\",\"messageType\":\"queda.confirmacao_solicitada\",\"userId\":7,\"episodeId\":\"e\",\"issuedAt\":\"x\",\"expiresAt\":\"amanha\",\"payload\":{\"timeoutMs\":5000,\"motivo\":\"q\"}}"},
      {"episodeId ausente", "{\"schemaVersion\":3,\"messageId\":\"bad-17\",\"messageType\":\"queda.confirmacao_solicitada\",\"userId\":7,\"issuedAt\":\"x\",\"expiresAt\":\"" + iso(g_agora_epoch + 60) + "\",\"payload\":{\"timeoutMs\":5000,\"motivo\":\"q\"}}"},
      {"nao e JSON", "isto nao e json"},
      {"JSON que nao e objeto", "[1,2,3]"},
    };
    for (auto& c : ruins) { g_rede_eventos.clear(); push(c.plano); avanca(30);
      CHECK(g_tone_pin == -1 && !has("push.ack"), std::string("push descartado: ") + c.nome); }
    // resultado invalido numa mensagem de encerramento: com o buzzer tocando, nao pode desliga-lo
    pushSolicitado("msg-toca", 20000); avanca(30);
    pushEncerrado("bad-res", "TALVEZ"); avanca(30);
    CHECK(g_tone_pin == PIN_BUZZER, "encerramento com resultado invalido nao desliga o buzzer");
    push(encerrada("ok-fim", "CANCELADO")); avanca(30);
    CHECK(g_tone_pin == -1, "o canal continua servindo depois das mensagens invalidas (encerramento valido desliga)");
    g_rede_eventos.clear();
    // envelope adulterado (tag), envelope sem campo, base64 invalido, nonce curto
    std::string e = cifrar(solicitada("bad-tag", "5000")); std::string adulterado = e; size_t p = adulterado.find("ciphertext") + 14; adulterado[p] = adulterado[p] == 'A' ? 'B' : 'A';
    pushBruto(adulterado); avanca(30);
    CHECK(g_tone_pin == -1 && !has("push.ack"), "envelope adulterado (tag nao confere) e descartado");
    pushBruto("{\"nonce\":\"AAAA\",\"ciphertext\":\"AAAA\"}"); pushBruto("{\"ciphertext\":\"AAAA\"}"); pushBruto("{\"nonce\":\"!!!\",\"ciphertext\":\"@@@\"}"); pushBruto("lixo"); avanca(60);
    CHECK(g_tone_pin == -1 && !has("push.ack"), "envelopes mal formados (nonce curto, campo ausente, base64 invalido, nao-JSON) sao descartados");
    // evento grande demais
    pushBruto(std::string(LINHA_SSE_MAX + 100, 'A')); avanca(60);
    CHECK(g_tone_pin == -1 && !has("push.ack"), "evento acima de LINHA_SSE_MAX e descartado inteiro, nunca truncado");
    pushSolicitado("msg-pos-ruins", 5000); avanca(30);
    CHECK(g_tone_pin == PIN_BUZZER && has("msg-pos-ruins"), "depois de tudo isso um pedido valido ainda funciona");
    bridge_off("CANCELADO"); avanca(30);
  }

  // ================= PUSH: enquadramento SSE/chunk =================
  { g_rede_eventos.clear();
    std::string ev = chunk(": keepalive\n\n") + chunk(eventoSse(cifrar(solicitada("frag-1", "5000"))));
    for (char c : ev) { g_sse += c; avanca(10); }                      // 1 byte por volta
    CHECK(g_tone_pin == PIN_BUZZER && has("frag-1"), "evento chega byte a byte e e montado");
    bridge_off("CANCELADO"); avanca(30);
    // dois eventos no mesmo bloco, chunk com extensao, chunk dividido no meio de um evento
    std::string e1 = eventoSse(cifrar(solicitada("multi-1", "5000"))), e2 = eventoSse(cifrar(encerrada("multi-2", "CANCELADO")));
    g_sse += chunk(e1) + chunk(e2); avanca(60);
    CHECK(g_tone_pin == -1 && has("multi-1"), "dois eventos num bloco: processa os dois, na ordem");
    g_rede_eventos.clear();
    std::string e3 = eventoSse(cifrar(solicitada("ext-1", "5000")));
    char h[32]; snprintf(h, sizeof(h), "%zx;ext=1\r\n", e3.size()); g_sse += std::string(h) + e3 + "\r\n"; avanca(40);
    CHECK(g_tone_pin == PIN_BUZZER && has("ext-1"), "chunk com extensao e aceito");
    bridge_off("CANCELADO"); avanca(30);
    std::string e4 = eventoSse(cifrar(solicitada("meio-1", "5000"))); size_t m = e4.size() / 2;
    g_sse += chunk(e4.substr(0, m)); avanca(40);
    CHECK(g_tone_pin == -1, "metade de um evento (chunk) nao atua");
    g_sse += chunk(e4.substr(m)); avanca(40);
    CHECK(g_tone_pin == PIN_BUZZER && has("meio-1"), "a outra metade completa o evento");
    bridge_off("CANCELADO"); avanca(30);
    // evento sem event: e evento desconhecido sao ignorados; id so e guardado depois de processar
    g_sse += chunk("data: x\n\n") + chunk("event: outro\ndata: y\n\n"); avanca(30);
    CHECK(g_tone_pin == -1, "eventos sem tipo ou desconhecidos sao ignorados");
  }
  // reconexao: chunk 0 encerra; reabre em 1 s com Last-Event-ID do ultimo evento PROCESSADO
  { int antes = g_push_aberturas; int ultimo = g_sse_id;
    g_sse += "0\r\n\r\n"; avanca(30);
    CHECK(!g_sse_aberto && g_push_aberturas == antes, "chunk 0 fecha o stream; a reabertura espera");
    avanca(900); CHECK(g_push_aberturas == antes, "antes de 1 s nao reabre");
    avanca(200);
    CHECK(g_push_aberturas == antes + 1 && g_push_ultimo_id == std::to_string(ultimo), "reabre em ~1 s com o Last-Event-ID do ultimo evento processado");
    g_sse_cai = true; avanca(30); CHECK(!g_sse_aberto, "queda do soquete tambem fecha"); g_sse_cai = false; avanca(1100);
    CHECK(g_sse_aberto, "e reabre");
    // backoff: falhas de abertura dobram a espera ate 10 s
    g_push_codigo = 503; g_sse_cai = true; avanca(30);
    std::vector<unsigned long> t; int a0 = g_push_aberturas; unsigned long ini = g_millis;
    for (int i = 0; i < 6; i++) { int a = g_push_aberturas; while (g_push_aberturas == a && g_millis - ini < 40000) avanca(10); t.push_back(g_millis - ini); }
    (void)a0;
    CHECK(t.size() == 6 && t[0] >= 900 && t[0] <= 1100 && t[1] - t[0] >= 900 && t[1] - t[0] <= 1100 && t[2] - t[1] >= 1900 && t[2] - t[1] <= 2100 &&
          t[3] - t[2] >= 3900 && t[3] - t[2] <= 4100 && t[4] - t[3] >= 7900 && t[4] - t[3] <= 8100 && t[5] - t[4] >= 9900 && t[5] - t[4] <= 10100,
          "queda: reabre em 1 s; falhas de abertura esperam 1, 2, 4, 8 e depois o teto de 10 s");
    g_push_codigo = 200; g_sse_cai = false; avanca(10500);
    CHECK(g_sse_aberto, "volta ao normal quando o servidor volta");
  }
  // reautenticar: renova e reabre na hora
  { int ref = g_refreshes, ab = g_push_aberturas; g_sse += chunk("event: reautenticar\ndata: x\n\n"); avanca(40);
    CHECK(g_refreshes == ref + 1 && g_push_aberturas == ab + 1 && g_sse_aberto, "evento reautenticar: renova os tokens e reabre o stream imediatamente");
    CHECK(g_push_bearer == "acc-" + std::to_string(g_logins + g_refreshes), "o stream reaberto usa o token novo"); }

  // ================= SESSAO: renovacao, 401, recusas, certificado =================
  { // renovacao antes de expirar: expires_in=3600, renova em 3300 s
    int ref = g_refreshes; reinicia(); Wire.az = 4096;
    avanca(3290000UL); CHECK(g_refreshes == ref, "antes de 5 min do vencimento nao renova");
    avanca(20000UL); CHECK(g_refreshes > ref, "renova o access token 5 min antes de vencer");
    CHECK(online(), "continua online depois da renovacao"); }
  { // 401 em /dados: renova uma vez e repete a mesma chamada; sem laco
    reinicia(); int ref = g_refreshes, antes = contaRota("/dados"); g_dados_codigo = 401; avanca(1100);
    CHECK(g_refreshes == ref + 1 && contaRota("/dados") - antes == 2, "401 em /dados: renova UMA vez e repete a chamada uma vez");
    g_dados_codigo = 200; }
  { // refresh recusado: apaga a sessao e refaz o login com a senha (sem trocar de certificado)
    reinicia(); int logins = g_logins, emis = g_emissoes; g_dados_codigo = 401; g_refresh_codigo = 401; avanca(1100);
    g_dados_codigo = 200; g_refresh_codigo = 200; avanca(300);
    CHECK(online() && g_logins == logins + 1 && g_emissoes == emis, "refresh recusado: novo login com a senha; o certificado e a chave sao reaproveitados");
    bool loginComCert = false; for (auto& c : g_chamadas) if (c.rota == "/auth/login" && c.comCertificado) loginComCert = true;
    CHECK(loginComCert, "com certificado ja emitido, o login tambem sai pela conexao com certificado"); }
  { // falhas de TLS com certificado: 3 seguidas -> nova emissao, com o MESMO token (sem novo login)
    reinicia(); int emis = g_emissoes, sem = g_sem_certificado, logins = g_logins;
    g_forcar_codigo = [](const std::string& r) { return (r == "/dados" || r == "/eventos") ? WIFI_ERRO_TLS : 0; };
    avanca(1100); CHECK(g_sem_certificado == sem && online(), "1 falha de handshake nao troca o certificado");
    for (int i = 0; i < 500 && g_sem_certificado == sem; i++) avanca(10);
    CHECK(g_sem_certificado == sem + 1 && !online(), "3 falhas seguidas: o certificado e descartado e nada protegido sai");
    g_forcar_codigo = nullptr; zeraRede(); avanca(4000);
    CHECK(contaRota("/auth/certificado") == 0 && contaRota("/dados") == 0, "ha espera antes de pedir o certificado novo (nao ha laco)");
    avanca(1500);
    CHECK(online() && g_emissoes == emis + 1 && g_logins == logins && g_cert_usado.find("FAKE" + std::to_string(g_emissoes)) != std::string::npos,
          "nova emissao automatica, com o mesmo token e a mesma chave (sem novo login), e volta a operar");
    { bool ok = true; for (auto& c : g_chamadas) if (c.rota == "/dados" && !c.comCertificado) ok = false;
      CHECK(ok, "durante a troca do certificado nao saiu /dados sem certificado"); } }
  { // a borda diz certificado_ausente (401): emite outro e repete SEM renovar o token nem refazer o login
    reinicia(); int sem = g_sem_certificado, ref = g_refreshes, logins = g_logins, emis = g_emissoes;
    g_servidor = [](const std::string& rota, const std::string&, const std::string&, std::string& saida) -> int {
      if (rota == "/dados") { saida = "{\"status\":\"erro\",\"codigo\":\"certificado_ausente\"}"; return 401; }
      saida = "{\"status\":\"ok\"}"; return 200; };
    for (int i = 0; i < 300 && g_sem_certificado == sem; i++) avanca(10);
    CHECK(g_sem_certificado == sem + 1 && g_refreshes == ref && g_logins == logins, "401 certificado_ausente: descarta o certificado, sem renovar o token nem refazer o login");
    servidorPadrao(); avanca(6000);
    CHECK(online() && g_emissoes == emis + 1 && g_logins == logins, "e emite outro com o mesmo token"); }
  { // 403 certificado_incompativel (certificado de outra conta ou device): descarta e emite outro
    reinicia(); int sem = g_sem_certificado, emis = g_emissoes;
    g_servidor = [](const std::string& rota, const std::string&, const std::string&, std::string& saida) -> int {
      if (rota == "/dados") { saida = "{\"status\":\"erro\",\"codigo\":\"certificado_incompativel\"}"; return 403; }
      saida = "{\"status\":\"ok\"}"; return 200; };
    for (int i = 0; i < 300 && g_sem_certificado == sem; i++) avanca(10);
    CHECK(g_sem_certificado == sem + 1, "403 certificado_incompativel: descarta o certificado");
    servidorPadrao(); avanca(12000);
    CHECK(online() && g_emissoes == emis + 1, "e emite outro"); }
  { // 403 sem o codigo do certificado (outra causa) NAO mexe no certificado
    reinicia(); int sem = g_sem_certificado;
    g_servidor = [](const std::string& rota, const std::string&, const std::string&, std::string& saida) -> int {
      if (rota == "/dados") { saida = "{\"status\":\"erro\",\"codigo\":\"fonte_invalida\"}"; return 403; }
      saida = "{\"status\":\"ok\"}"; return 200; };
    avanca(3300); CHECK(g_sem_certificado == sem && online(), "403 por outro motivo (fonte_invalida) nao descarta o certificado");
    servidorPadrao(); }
  { // push: a borda recusa a abertura por causa do certificado / do token
    reinicia(); g_sse_cai = true; avanca(30); int sem = g_sem_certificado, ref = g_refreshes, emis = g_emissoes;
    g_push_codigo = 401; g_push_erro = "{\"codigo\":\"token_invalido\"}";
    avanca(1100); CHECK(g_refreshes > ref && g_sem_certificado == sem, "push 401 por token vencido: renova o token (e nao mexe no certificado)");
    g_push_codigo = 401; g_push_erro = "{\"codigo\":\"certificado_ausente\"}"; ref = g_refreshes;
    for (int i = 0; i < 1500 && g_sem_certificado == sem; i++) avanca(10);
    CHECK(g_sem_certificado == sem + 1 && g_refreshes == ref, "push 401 certificado_ausente: descarta o certificado, sem renovar o token");
    g_push_codigo = 200; g_push_erro.clear(); g_sse_cai = false; avanca(30000);
    CHECK(online() && g_emissoes == emis + 1 && g_sse_aberto, "emite outro e o push reabre"); }
  { // emissao: 503 e 400 (CSR recusado) esperam cada vez mais; 400 nao repete em laco
    g_forcar_codigo = [](const std::string& r) { return (r == "/dados" || r == "/eventos") ? WIFI_ERRO_TLS : 0; };
    { int sem = g_sem_certificado; for (int i = 0; i < 600 && g_sem_certificado == sem; i++) avanca(10); }
    g_forcar_codigo = nullptr; g_emissao_codigo = 503; zeraRede(); avanca(40000);
    int n503 = contaRota("/auth/certificado");
    CHECK(n503 >= 3 && n503 <= 4 && !online() && contaRota("/dados") == 0, "emissao com 503: espera crescente (5, 10, 20 s...) e nada protegido sai");
    g_emissao_codigo = 200; avanca(70000); CHECK(online(), "a emissao volta e o firmware fica online");
    g_forcar_codigo = [](const std::string& r) { return (r == "/dados" || r == "/eventos") ? WIFI_ERRO_TLS : 0; };
    { int sem = g_sem_certificado; for (int i = 0; i < 600 && g_sem_certificado == sem; i++) avanca(10); }
    g_forcar_codigo = nullptr; g_emissao_codigo = 400; zeraRede(); avanca(50000);
    CHECK(contaRota("/auth/certificado") == 1, "400 csr_invalido: uma tentativa e espera longa (erro de programacao, sem laco)");
    CHECK(g_chamadas[0].plano.find("\"schemaVersion\":1") != std::string::npos, "o pedido de certificado leva schemaVersion 1");
    g_emissao_codigo = 200; avanca(70000); CHECK(online(), "e se recupera depois"); }
  { // servidor fora do ar: nada trava; volta sozinho
    reinicia(); g_wifi_ok = false; avanca(3000); CHECK(g_chamadas.empty(), "sem Wi-Fi nenhuma chamada sai");
    g_wifi_ok = true; avanca(1200); CHECK(has("imobilidade.leitura"), "voltou o Wi-Fi: os eventos voltam a sair"); }
  { // login recusado repete a cada 5 s, sem avancar nem enviar nada
    g_login_codigo = 401; g_dados_codigo = 401; g_refresh_codigo = 401; avanca(1100); zeraRede();
    avanca(11000); int l = contaRota("/auth/login");
    CHECK(l >= 2 && l <= 3 && contaRota("/auth/certificado") == 0 && contaRota("/dados") == 0 && !online(), "login recusado: repete a cada 5 s, sem avancar nem enviar nada");
    g_login_codigo = 200; g_dados_codigo = 200; g_refresh_codigo = 200; avanca(6000); CHECK(online(), "o login volta e o firmware fica online"); }
  { // sem horario (NTP): nada e enviado
    reinicia(); g_relogio_ok = false; avanca(8000);
    CHECK(g_chamadas.empty() && !has("imobilidade"), "sem horario valido nada e enviado (nem eventos, nem push, nem renovacao)");
    g_relogio_ok = true; avanca(2000); CHECK(has("imobilidade.leitura") && online(), "com o horario, tudo segue"); }

  // ================= Serial e segredos =================
  CHECK(Serial.out.find("segredo-teste") == std::string::npos && Serial.out.find("eventType") == std::string::npos && Serial.out.find("acc-") == std::string::npos &&
        Serial.out.find("ref-") == std::string::npos && Serial.out.find("PRIVATE KEY") == std::string::npos && Serial.out.find("AAECAwQFBgc") == std::string::npos,
        "a Serial nao carrega segredo, token, chave, evento nem push");
  { bool segredoSo = true; for (auto& c : g_chamadas) if (c.rota != "/auth/login" && c.plano.find("segredo-teste") != std::string::npos) segredoSo = false;
    CHECK(segredoSo, "o segredo so aparece no corpo do login"); }

  // ================= Envelope: vetores e formato =================
  { // mesmo vetor que gerar_vetores.py (Python/cryptography) confere: chave 0x00..0x1f, nonce fixo
    static const uint8_t nonce[12] = {0,1,2,3,4,5,6,7,8,9,10,11}; g_aleatorio_fixo = nonce;
    char e[ENV_JSON_MAX]; const char* t = "{\"x\":1}"; int n = envelopeCifrar(t, strlen(t), e, sizeof(e)); g_aleatorio_fixo = nullptr;
    { std::ofstream(pastaVetores() + "/flow_env.json") << std::string(e, n); }
    CHECK(n > 0 && strstr(e, "\"nonce\":\"AAECAwQFBgcICQoL\"") != nullptr, "envelope: nonce de 12 bytes em base64 e formato {nonce, ciphertext}");
    char saida[64]; int d = envelopeDecifrar(e, n, saida, sizeof(saida));
    CHECK(d == (int)strlen(t) && strcmp(saida, t) == 0, "envelope: ida e volta");
    std::string adulterado(e, n); size_t p = adulterado.find("ciphertext") + 14; adulterado[p] ^= 1;
    CHECK(envelopeDecifrar(adulterado.c_str(), adulterado.size(), saida, sizeof(saida)) < 0, "envelope: tag adulterada e recusada");
    char pequeno[3]; CHECK(envelopeDecifrar(e, n, pequeno, sizeof(pequeno)) < 0, "envelope: buffer de saida pequeno e erro, nunca texto cortado");
    char a[ENV_JSON_MAX], b[ENV_JSON_MAX]; envelopeCifrar(t, strlen(t), a, sizeof(a)); envelopeCifrar(t, strlen(t), b, sizeof(b));
    CHECK(strcmp(a, b) != 0, "envelope: nonce muda a cada mensagem"); }
  { std::string grande(ENV_TEXTO_MAX + 1, 'a'); char e[ENV_JSON_MAX]; CHECK(envelopeCifrar(grande.c_str(), grande.size(), e, sizeof(e)) < 0, "envelope: texto acima do limite e recusado");
    CHECK(!envelopeIniciarCom("AAAA") && !g_aesPronta, "chave AES de tamanho errado e recusada");
    CHECK(envelopeIniciar() && g_aesPronta, "e a chave certa volta a funcionar"); }
  { // vetor feito pelo Python (borda/crypto_utils.py), se verificar_contra_python.py o deixou na pasta
    std::ifstream f(pastaVetores() + "/flow_py_env.json");
    if (f) { std::string env((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>()); char saida[ENV_TEXTO_MAX + 1];
      int d = envelopeDecifrar(env.c_str(), env.size(), saida, sizeof(saida));
      CHECK(d > 0, "envelope feito pelo servidor (Python) e decifrado pelo firmware");
      if (d > 0) std::ofstream(pastaVetores() + "/flow_py_resultado.txt") << std::string(saida, d); } }
  { // data/ISO
    long long e; CHECK(pushIsoParaEpoch("2026-11-22T12:34:56.789Z", &e) && e == 1795350896LL, "ISO 8601 -> epoch (UTC)");
    CHECK(!pushIsoParaEpoch("2026-11-22 12:34:56.789Z", &e) && !pushIsoParaEpoch("2026-13-22T12:34:56.789Z", &e) && !pushIsoParaEpoch("", &e), "ISO malformado e recusado"); }

  std::cout << (fails ? "\nFALHAS: " : "\nFIRMWARE OK ") << fails << "\n";
  return fails;
}
