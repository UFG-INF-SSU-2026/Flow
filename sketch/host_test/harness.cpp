#include "Arduino.h"
#include "sketch_host.cpp"
#include <iostream>
#include <cassert>
static int fails = 0;
#define CHECK(c, m) do { if (c) std::cout << "PASS " << m << "\n"; else { std::cout << "FAIL " << m << "\n"; fails++; } } while (0)
static bool has(const char* t) { return Serial.out.find(t) != std::string::npos; }
static void avanca(unsigned long ms) { for (unsigned long i = 0; i < ms; i += 10) { g_millis += 10; loop(); } }
int main() {
  g_pin[PIN_BTN_CONFIRM] = HIGH; g_pin[PIN_BTN_REARM] = HIGH;
  setup();
  CHECK(Wire.accelCfg == 0x10, "setup escreve ACCEL_CONFIG=0x10 (+-8g)");
  Serial.out.clear();
  // leitura: 1 g no eixo Z em +-8g = 4096 LSB -> 9.81 m/s2
  Wire.az = 4096; avanca(1100);
  CHECK(has("\"eventType\":\"imobilidade.leitura\""), "emite imobilidade.leitura");
  CHECK(has("\"value\":9.81") && has("\"valueMax\":9.81"), "value e valueMax corretos com divisor 4096");
  CHECK(has("\"schemaVersion\":3"), "schemaVersion 3");
  // pico entre ciclos: sobe o eixo por 100 ms e volta
  Serial.out.clear(); Wire.az = 0; avanca(500); Wire.az = 16384; avanca(100); Wire.az = 0; avanca(500);
  CHECK(has("\"valueMax\":39.2") || has("\"valueMax\":39.3") || has("\"valueMax\":39.22"), "valueMax captura o pico (4 g) amostrado a cada 50 ms");
  // comando BUZZER_ON
  Serial.out.clear();
  Serial.feed("CMD;BUZZER_ON;msg-123;5000\n"); avanca(30);
  CHECK(g_tone_pin == PIN_BUZZER && g_tone_freq == BUZZER_FREQ_HZ, "BUZZER_ON liga tone no pino do buzzer");
  CHECK(has("\"eventType\":\"push.ack\"") && has("\"state\":\"ATIVADO\"") && has("\"messageId\":\"msg-123\""), "emite push.ack ATIVADO com messageId");
  // botao Estou bem (acoplado a pressConfirm tambem)
  Serial.out.clear();
  g_pin[PIN_BTN_CONFIRM] = LOW; avanca(120); g_pin[PIN_BTN_CONFIRM] = HIGH; avanca(60);
  CHECK(g_tone_pin == -1, "botao Estou bem desliga o buzzer");
  CHECK(has("\"eventType\":\"confirmacao.resposta\"") && has("\"state\":\"ESTOU_BEM\"") && has("msg-123"), "emite confirmacao.resposta ESTOU_BEM");
  // timeout
  Serial.out.clear();
  Serial.feed("CMD;BUZZER_ON;msg-456;1500\n"); avanca(30);
  CHECK(g_tone_pin == PIN_BUZZER, "segundo BUZZER_ON liga");
  avanca(1400); CHECK(g_tone_pin == PIN_BUZZER, "ainda toca antes do timeout");
  avanca(300);
  CHECK(g_tone_pin == -1 && has("\"state\":\"SEM_RESPOSTA\"") && has("msg-456"), "timeout desliga e emite SEM_RESPOSTA");
  // BUZZER_OFF do servidor
  Serial.out.clear();
  Serial.feed("CMD;BUZZER_ON;msg-789;5000\n"); avanca(30);
  Serial.feed("CMD;BUZZER_OFF;msg-off;CONFIRMADO\n"); avanca(30);
  CHECK(g_tone_pin == -1, "BUZZER_OFF desliga");
  CHECK(!has("confirmacao.resposta"), "BUZZER_OFF nao emite evento de resposta");
  // comando invalido e teto
  Serial.out.clear();
  Serial.feed("CMD;BUZZER_ON;msg-x;999999\n"); avanca(30);
  CHECK(g_tone_pin == -1 && has("\"state\":\"REJEITADO\""), "timeout acima do teto -> REJEITADO e buzzer desligado");
  Serial.out.clear();
  Serial.feed("lixo qualquer\nCMD;BUZZER_ON\nCMD;BUZZER_ON;;100\n"); avanca(30);
  CHECK(g_tone_pin == -1, "linhas invalidas nao ligam o buzzer");
  // clique sem buzzer nao gera resposta
  Serial.out.clear();
  g_pin[PIN_BTN_CONFIRM] = LOW; avanca(120); g_pin[PIN_BTN_CONFIRM] = HIGH; avanca(60);
  CHECK(!has("confirmacao.resposta"), "clique sem buzzer ativo nao emite resposta");
  // fora de faixa
  Serial.out.clear(); Wire.az = 32767; Wire.ax = 32767; Wire.ay = 32767; avanca(1100);
  CHECK(!has("LEITURA_FORA_DE_FAIXA"), "~136 m/s2 (fundo de escala +-8g) < teto 160: nao e fora de faixa");
  std::cout << (fails ? "\nFALHAS: " : "\nFIRMWARE OK ") << fails << "\n";
  return fails;
}
