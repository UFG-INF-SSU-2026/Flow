/*
  Atividade 03 - Software para Sistemas Ubíquos
  Recorte individual: DECISÃO E ATUAÇÃO
  Cenário: Monitoramento e assistência a uma pessoa idosa

  Responsabilidade: Decisão e atuação
    - histerese (dois limiares)
    - persistência da condição por período mínimo
    - confirmação humana ("estou bem")
    - rearme manual
    - atuação segura (alarme travado, sem disparos repetidos)

  Sensor: MPU6050 via Wire (I2C direto, sem biblioteca externa)
  Leitura baseada no exemplo oficial do Wokwi (Koepel, 2021)
  Wire.begin() sem pinos → usa padrão ESP32: SDA=GPIO21, SCL=GPIO22

  Limitações:
  - MPU6050 simulado não replica ruído real nem deriva térmica.
  - eventTimeMs = millis() desde início da simulação (sem RTC).
  - Ciclo de leitura/decisão a 1x/s, controlado por millis() (não-bloqueante); botões são lidos a cada volta do loop.
  - No simulador, repouso = todos os sliders zerados = magnitude ~0 m/s².
    Em hardware real, o eixo Z leria ~9.8 m/s² (gravidade).
*/

#include <Wire.h>

// ---- MPU6050 ----
const int MPU_ADDR = 0x68;

// ---- Pinos ----
#define PIN_BTN_CONFIRM   25
#define PIN_BTN_REARM     26
#define PIN_LED_SUSPEITA  27
#define PIN_LED_ALERTA    14
#define PIN_BUZZER        13   // buzzer (wokwi-buzzer): terminal 1 -> GND, terminal 2 -> D13

const char* DEVICE_ID = "esp32-decisao-imobilidade-01";
const char* ENTITY_ID = "idoso-simulado-01";

// ---- Versao do schema do payload (docs/schemas.md, secao 1) ----
const int SCHEMA_VERSION = 3;

// ---- Faixa do sensor (Marco 3) ----
// MPU6050 em +-8 g: ACCEL_CONFIG (0x1C) = 0x10 -> 4096 LSB/g. Teto plausivel de 160 m/s2 (igual ao do app).
// Em +-8 g a magnitude maxima mensuravel e ~136 m/s2, entao o teto e um limite de contrato, nao atingivel.
const uint8_t ACCEL_CONFIG_8G      = 0x10;
const float   ACCEL_LSB_POR_G      = 4096.0;
const float   MAGNITUDE_MAX_PLAUSIVEL = 160.0;
const unsigned long AMOSTRA_MS     = 50;   // amostragem rapida entre ciclos, para capturar o PICO (valueMax)

// ---- Buzzer (Marco 3) ----
const unsigned int  BUZZER_FREQ_HZ = 2000;
const unsigned long BUZZER_MAX_MS  = 30000; // TETO DE SEGURANCA do firmware: o buzzer nunca passa disso,
                                            // mesmo que servidor/bridge morram. NAO e o timeout de confirmacao
                                            // (esse vem na mensagem, em timeoutMs).
                                            // INVARIANTE: TIMEOUT_CONFIRMACAO_S * 1000 (SensorServer/const.py) precisa
                                            // ser <= BUZZER_MAX_MS. Acima disso o firmware rejeita TODO pedido
                                            // (push.ack REJEITADO) e o buzzer nunca toca. E a excecao a regra de que
                                            // o timeout vem so do servidor: ajuste as duas juntas.

// ---- Histerese em m/s² ----
// Repouso no Wokwi: sliders zerados → magnitude ≈ 0 m/s²
// Para simular imobilidade: deixe todos os sliders em zero
// Para simular movimento: mova qualquer slider acima de 3 m/s²
const float LIMIAR_BAIXO = 2.0;
const float LIMIAR_ALTO  = 5.0;

// ---- Persistência / janelas ----
const unsigned long T_PERSISTENCIA_MS = 5000;
const unsigned long T_CONFIRMACAO_MS  = 8000;
const unsigned long DEBOUNCE_MS       = 50;

// ---- Estado ----
enum EstadoSistema { NORMAL, AGUARDANDO_CONFIRMACAO, ALERTA_CONFIRMADO };
EstadoSistema estado = NORMAL;

const char* nomeEstado(EstadoSistema e) {
  switch (e) {
    case NORMAL:                 return "NORMAL";
    case AGUARDANDO_CONFIRMACAO: return "AGUARDANDO_CONFIRMACAO";
    case ALERTA_CONFIRMADO:      return "ALERTA_CONFIRMADO";
  }
  return "DESCONHECIDO";
}

bool contandoPersistencia = false;
unsigned long tInicioBaixo = 0;
unsigned long tInicioConfirmacao = 0;
uint32_t sequence = 0;

// ---- Ciclo de leitura/decisão não-bloqueante (1x/s) ----
unsigned long tUltimoCiclo = 0;
const unsigned long CICLO_MS = 1000;

// ---- Cliques pendentes (capturados a cada volta do loop, aplicados no próximo ciclo) ----
bool pendingConfirm = false;
bool pendingRearm   = false;
bool pendingBuzzerConfirm = false;   // clique em "Estou bem" enquanto o buzzer toca (tratado a cada volta do loop)

// ---- Buzzer / comandos recebidos pela serial (Marco 3) ----
bool buzzerAtivo = false;
unsigned long tBuzzerInicio = 0;
unsigned long buzzerTimeoutMs = 0;
char buzzerMsgId[40] = "";
char cmdBuf[96];
uint8_t cmdLen = 0;

// ---- Pico de aceleracao entre ciclos (valueMax) ----
unsigned long tUltimaAmostra = 0;
float magMaxCiclo = 0.0;

// ---- Debounce ----
int lastRawConfirm = HIGH, stableConfirm = HIGH;
unsigned long lastChangeConfirm = 0;
int lastRawRearm = HIGH, stableRearm = HIGH;
unsigned long lastChangeRearm = 0;

// ---- Variáveis do MPU6050 ----
int16_t AcX, AcY, AcZ;

bool lerMPU() {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x3B); // registrador ACCEL_XOUT_H
  if (Wire.endTransmission(false) != 0) return false; // false = repeated start
  
  if (Wire.requestFrom(MPU_ADDR, 6) != 6) return false;
  
  AcX = Wire.read() << 8 | Wire.read();
  AcY = Wire.read() << 8 | Wire.read();
  AcZ = Wire.read() << 8 | Wire.read();
  return true;
}

float calcMagnitude() {
  // Converte para m/s² (±8g configurado no setup: 4096 LSB/g)
  float ax = (AcX / ACCEL_LSB_POR_G) * 9.80665;
  float ay = (AcY / ACCEL_LSB_POR_G) * 9.80665;
  float az = (AcZ / ACCEL_LSB_POR_G) * 9.80665;
  return sqrt(ax*ax + ay*ay + az*az);
}

// valueMax < 0 = nao incluir o campo (so imobilidade.leitura envia o pico do ciclo)
void emitirEvento(const char* eventType, const char* estadoTexto, float value, float valueMax = -1.0) {
  sequence++;
  Serial.print("{");
  Serial.print("\"schemaVersion\":"); Serial.print(SCHEMA_VERSION); Serial.print(",");
  Serial.print("\"eventType\":\""); Serial.print(eventType); Serial.print("\",");
  Serial.print("\"deviceId\":\""); Serial.print(DEVICE_ID); Serial.print("\",");
  Serial.print("\"entityId\":\""); Serial.print(ENTITY_ID); Serial.print("\",");
  Serial.print("\"eventTimeMs\":"); Serial.print(millis()); Serial.print(",");
  Serial.print("\"sequence\":"); Serial.print(sequence); Serial.print(",");
  Serial.print("\"value\":"); Serial.print(value, 2); Serial.print(",");
  if (valueMax >= 0.0) { Serial.print("\"valueMax\":"); Serial.print(valueMax, 2); Serial.print(","); }
  Serial.print("\"unit\":\"m/s2\",");
  Serial.print("\"state\":\""); Serial.print(estadoTexto); Serial.print("\"");
  Serial.println("}");
}

// Evento de retorno (push.ack / confirmacao.resposta): o bridge o envia a POST /eventos.
void emitirEventoRetorno(const char* eventType, const char* estadoTexto, const char* messageId) {
  sequence++;
  Serial.print("{");
  Serial.print("\"schemaVersion\":"); Serial.print(SCHEMA_VERSION); Serial.print(",");
  Serial.print("\"eventType\":\""); Serial.print(eventType); Serial.print("\",");
  Serial.print("\"deviceId\":\""); Serial.print(DEVICE_ID); Serial.print("\",");
  Serial.print("\"entityId\":\""); Serial.print(ENTITY_ID); Serial.print("\",");
  Serial.print("\"eventTimeMs\":"); Serial.print(millis()); Serial.print(",");
  Serial.print("\"sequence\":"); Serial.print(sequence); Serial.print(",");
  Serial.print("\"messageId\":\""); Serial.print(messageId); Serial.print("\",");
  Serial.print("\"state\":\""); Serial.print(estadoTexto); Serial.print("\"");
  Serial.println("}");
}

void desligarBuzzer() {
  noTone(PIN_BUZZER);
  buzzerAtivo = false;
  digitalWrite(PIN_LED_ALERTA, LOW);   // LED vermelho acompanha o pedido do servidor
}

void ligarBuzzer(const char* messageId, long timeoutMs) {
  // Idempotencia (docs/schemas.md, secao 6: o dispositivo ignora messageId repetido): com o buzzer tocando, o MESMO
  // messageId nao reinicia o tempo nem reemite push.ack. Um messageId DIFERENTE substitui o episodio em curso (vale o
  // pedido mais novo): reinicia o tempo, emite push.ack ATIVADO para o novo e nao emite SEM_RESPOSTA para o anterior
  // (o servidor ja encerra ou expira o episodio antigo por conta propria).
  if (buzzerAtivo && strcmp(messageId, buzzerMsgId) == 0) return;

  // Valida: timeout em (0, BUZZER_MAX_MS] e messageId que cabe no buffer
  if (timeoutMs <= 0 || (unsigned long)timeoutMs > BUZZER_MAX_MS || strlen(messageId) == 0 || strlen(messageId) >= sizeof(buzzerMsgId)) {
    emitirEventoRetorno("push.ack", "REJEITADO", messageId);
    return;
  }
  strncpy(buzzerMsgId, messageId, sizeof(buzzerMsgId) - 1);
  buzzerMsgId[sizeof(buzzerMsgId) - 1] = '\0';
  buzzerTimeoutMs = (unsigned long)timeoutMs;
  tBuzzerInicio = millis();
  buzzerAtivo = true;
  pendingBuzzerConfirm = false;
  tone(PIN_BUZZER, BUZZER_FREQ_HZ);
  digitalWrite(PIN_LED_ALERTA, HIGH);  // LED vermelho: so liga quando o servidor pede (BUZZER_ON)
  emitirEventoRetorno("push.ack", "ATIVADO", messageId);
}

// Linha de comando do bridge:  CMD;BUZZER_ON;<messageId>;<timeoutMs>   |   CMD;BUZZER_OFF;<messageId>;<motivo>
void processarComando(char* linha) {
  char* ctx = nullptr;
  char* t0 = strtok_r(linha, ";", &ctx);
  char* t1 = strtok_r(nullptr, ";", &ctx);
  char* t2 = strtok_r(nullptr, ";", &ctx);
  char* t3 = strtok_r(nullptr, ";", &ctx);
  if (!t0 || strcmp(t0, "CMD") != 0 || !t1 || !t2) return;   // linha que nao e comando: ignora
  if (strcmp(t1, "BUZZER_ON") == 0 && t3) {
    ligarBuzzer(t2, atol(t3));
  } else if (strcmp(t1, "BUZZER_OFF") == 0) {
    if (buzzerAtivo) desligarBuzzer();                       // encerrado pelo servidor: desliga sem evento
  }
}

// Le a serial sem bloquear, acumulando ate '\n'.
void lerComandosSerial() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      cmdBuf[cmdLen] = '\0';
      if (cmdLen > 0) processarComando(cmdBuf);
      cmdLen = 0;
    } else if (cmdLen < sizeof(cmdBuf) - 1) {
      cmdBuf[cmdLen++] = c;
    } else {
      cmdLen = 0;                                            // linha longa demais: descarta
    }
  }
}

// Resposta do idoso / fim do tempo, avaliado a cada volta do loop (nao espera o ciclo de 1 s).
void atenderBuzzer() {
  if (!buzzerAtivo) { pendingBuzzerConfirm = false; return; }
  if (pendingBuzzerConfirm) {
    pendingBuzzerConfirm = false;
    desligarBuzzer();
    emitirEventoRetorno("confirmacao.resposta", "ESTOU_BEM", buzzerMsgId);
  } else if (millis() - tBuzzerInicio >= buzzerTimeoutMs) {
    desligarBuzzer();
    emitirEventoRetorno("confirmacao.resposta", "SEM_RESPOSTA", buzzerMsgId);
  }
}

bool debounceRead(int pin, int &lastRaw, int &stable, unsigned long &lastChange) {
  int raw = digitalRead(pin);
  if (raw != lastRaw) { lastChange = millis(); lastRaw = raw; }
  if ((millis() - lastChange) > DEBOUNCE_MS && raw != stable) {
    stable = raw;
    return true;
  }
  return false;
}

void setup() {
  Serial.begin(115200);
  while (!Serial) delay(10); // aguarda serial ficar pronta

  pinMode(PIN_BTN_CONFIRM,  INPUT_PULLUP);
  pinMode(PIN_BTN_REARM,    INPUT_PULLUP);
  pinMode(PIN_LED_SUSPEITA, OUTPUT);
  pinMode(PIN_LED_ALERTA,   OUTPUT);
  pinMode(PIN_BUZZER,       OUTPUT);
  noTone(PIN_BUZZER);

  digitalWrite(PIN_LED_SUSPEITA, LOW);
  digitalWrite(PIN_LED_ALERTA,   LOW);

  // Inicializa I2C com pinos padrão do ESP32 (SDA=21, SCL=22)
  Wire.begin();

  // Acorda o MPU6050 (sai do modo sleep)
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x6B); // PWR_MGMT_1
  Wire.write(0x00); // 0 = acorda o sensor
  int err = Wire.endTransmission();

  // Marco 3: faixa do acelerometro em +-8 g (ACCEL_CONFIG = 0x10)
  if (err == 0) {
    Wire.beginTransmission(MPU_ADDR);
    Wire.write(0x1C);              // ACCEL_CONFIG
    Wire.write(ACCEL_CONFIG_8G);
    err = Wire.endTransmission();
  }

  if (err != 0) {
    Serial.print("{\"schemaVersion\":"); Serial.print(SCHEMA_VERSION);
    Serial.print(",\"eventType\":\"sistema.erro\",\"state\":\"MPU_NAO_ENCONTRADO\",\"code\":");
    Serial.print(err);
    Serial.println("}");
    while (1) delay(100);
  }

  Serial.println("MPU6050 encontrado e iniciado!");
  Serial.println("Protótipo iniciado - perfil: Decisão e atuação (MPU6050)");
  Serial.println("Repouso = sliders zerados. Movimento = qualquer slider acima de 3 m/s²");
}

void loop() {
  // --- Leitura dos botões a cada volta do loop (rápida, sem delay) ---
  // Garante que um único clique seja capturado, sem precisar segurar o botão.
  bool bordaConfirm = debounceRead(PIN_BTN_CONFIRM, lastRawConfirm, stableConfirm, lastChangeConfirm);
  if (bordaConfirm && stableConfirm == LOW) {
    pendingConfirm = true;                       // maquina de imobilidade (Marco 2), consumido no ciclo de 1 s
    if (buzzerAtivo) pendingBuzzerConfirm = true; // resposta ao pedido do servidor (Marco 3), tratado ja
  }

  bool bordaRearm = debounceRead(PIN_BTN_REARM, lastRawRearm, stableRearm, lastChangeRearm);
  if (bordaRearm && stableRearm == LOW) pendingRearm = true;

  // --- Marco 3: comandos do servidor (via bridge) e buzzer, a cada volta do loop ---
  lerComandosSerial();
  atenderBuzzer();

  // --- Marco 3: amostragem rapida do MPU para capturar o PICO entre ciclos (valueMax) ---
  unsigned long agora = millis();
  if (agora - tUltimaAmostra >= AMOSTRA_MS) {
    tUltimaAmostra = agora;
    if (lerMPU()) {
      float m = calcMagnitude();
      if (m > magMaxCiclo) magMaxCiclo = m;
    }
  }

  // --- Ciclo de leitura do sensor / decisão: só executa 1x por segundo ---
  if (agora - tUltimoCiclo < CICLO_MS) return;
  tUltimoCiclo = agora;

  if (!lerMPU()) {
    emitirEvento("imobilidade.decisao", "LEITURA_INVALIDA", -1.0);
    return;
  }

  float magnitude = calcMagnitude();
  float valueMax = (magMaxCiclo > magnitude) ? magMaxCiclo : magnitude;   // pico do ciclo (>= value)
  magMaxCiclo = 0.0;

  // Heartbeat: emite a leitura + pico + estado atual da máquina a cada ciclo (1x/s)
  emitirEvento("imobilidade.leitura", nomeEstado(estado), magnitude, valueMax);

  // Faixa plausível: 0 a 160 m/s²
  if (magnitude > MAGNITUDE_MAX_PLAUSIVEL) {
    emitirEvento("imobilidade.decisao", "LEITURA_FORA_DE_FAIXA", magnitude);
    return;
  }

  // Consome os cliques capturados desde o ciclo anterior
  bool pressConfirm = pendingConfirm; pendingConfirm = false;
  bool pressRearm   = pendingRearm;   pendingRearm   = false;

  switch (estado) {
    case NORMAL: {
      digitalWrite(PIN_LED_SUSPEITA, LOW);

      if (magnitude < LIMIAR_BAIXO) {
        if (!contandoPersistencia) {
          contandoPersistencia = true;
          tInicioBaixo = agora;
        } else if (agora - tInicioBaixo >= T_PERSISTENCIA_MS) {
          estado = AGUARDANDO_CONFIRMACAO;
          tInicioConfirmacao = agora;
          contandoPersistencia = false;
          emitirEvento("imobilidade.decisao", "SUSPEITA_IMOBILIDADE", magnitude);
        }
      } else {
        contandoPersistencia = false;
      }
      break;
    }
    case AGUARDANDO_CONFIRMACAO: {
      digitalWrite(PIN_LED_SUSPEITA, HIGH);
      if (pressConfirm) {
        estado = NORMAL;
        emitirEvento("imobilidade.decisao", "CONFIRMADO_OK", magnitude);
        break;
      }
      if (magnitude > LIMIAR_ALTO) {
        estado = NORMAL;
        emitirEvento("imobilidade.decisao", "MOVIMENTO_RETOMADO", magnitude);
        break;
      }
      if (agora - tInicioConfirmacao >= T_CONFIRMACAO_MS) {
        estado = ALERTA_CONFIRMADO;
        emitirEvento("imobilidade.decisao", "ALERTA_IMOBILIDADE", magnitude);
      }
      break;
    }
    case ALERTA_CONFIRMADO: {
      // Alerta local: LED AMARELO (o vermelho so liga quando o servidor envia o comando)
      digitalWrite(PIN_LED_SUSPEITA, HIGH);
      if (pressRearm) {
        estado = NORMAL;
        contandoPersistencia = false;
        emitirEvento("imobilidade.decisao", "REARMADO_MANUAL", magnitude);
      }
      break;
    }
  }
}
