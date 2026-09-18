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

const char* DEVICE_ID = "esp32-decisao-imobilidade-01";
const char* ENTITY_ID = "idoso-simulado-01";

// ---- Versao do schema do payload (docs/arquitetura.md, secao 4.3) ----
const int SCHEMA_VERSION = 2;

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
  // Converte para m/s² (sensibilidade padrão ±2g = 16384 LSB/g)
  float ax = (AcX / 16384.0) * 9.80665;
  float ay = (AcY / 16384.0) * 9.80665;
  float az = (AcZ / 16384.0) * 9.80665;
  return sqrt(ax*ax + ay*ay + az*az);
}

void emitirEvento(const char* eventType, const char* estadoTexto, float value) {
  sequence++;
  Serial.print("{");
  Serial.print("\"schemaVersion\":"); Serial.print(SCHEMA_VERSION); Serial.print(",");
  Serial.print("\"eventType\":\""); Serial.print(eventType); Serial.print("\",");
  Serial.print("\"deviceId\":\""); Serial.print(DEVICE_ID); Serial.print("\",");
  Serial.print("\"entityId\":\""); Serial.print(ENTITY_ID); Serial.print("\",");
  Serial.print("\"eventTimeMs\":"); Serial.print(millis()); Serial.print(",");
  Serial.print("\"sequence\":"); Serial.print(sequence); Serial.print(",");
  Serial.print("\"value\":"); Serial.print(value, 2); Serial.print(",");
  Serial.print("\"unit\":\"m/s2\",");
  Serial.print("\"state\":\""); Serial.print(estadoTexto); Serial.print("\"");
  Serial.println("}");
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

  digitalWrite(PIN_LED_SUSPEITA, LOW);
  digitalWrite(PIN_LED_ALERTA,   LOW);

  // Inicializa I2C com pinos padrão do ESP32 (SDA=21, SCL=22)
  Wire.begin();

  // Acorda o MPU6050 (sai do modo sleep)
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x6B); // PWR_MGMT_1
  Wire.write(0x00); // 0 = acorda o sensor
  int err = Wire.endTransmission();

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
  if (bordaConfirm && stableConfirm == LOW) pendingConfirm = true;

  bool bordaRearm = debounceRead(PIN_BTN_REARM, lastRawRearm, stableRearm, lastChangeRearm);
  if (bordaRearm && stableRearm == LOW) pendingRearm = true;

  // --- Ciclo de leitura do sensor / decisão: só executa 1x por segundo ---
  unsigned long agora = millis();
  if (agora - tUltimoCiclo < CICLO_MS) return;
  tUltimoCiclo = agora;

  if (!lerMPU()) {
    emitirEvento("imobilidade.decisao", "LEITURA_INVALIDA", -1.0);
    return;
  }

  float magnitude = calcMagnitude();

  // Heartbeat: emite a leitura + estado atual da máquina a cada ciclo (1x/s)
  emitirEvento("imobilidade.leitura", nomeEstado(estado), magnitude);

  // Faixa plausível: 0 a 30 m/s²
  if (magnitude > 30.0) {
    emitirEvento("imobilidade.decisao", "LEITURA_FORA_DE_FAIXA", magnitude);
    return;
  }

  // Consome os cliques capturados desde o ciclo anterior
  bool pressConfirm = pendingConfirm; pendingConfirm = false;
  bool pressRearm   = pendingRearm;   pendingRearm   = false;

  switch (estado) {
    case NORMAL: {
      digitalWrite(PIN_LED_SUSPEITA, LOW);
      digitalWrite(PIN_LED_ALERTA,   LOW);

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
      digitalWrite(PIN_LED_SUSPEITA, LOW);
      digitalWrite(PIN_LED_ALERTA,   HIGH);
      if (pressRearm) {
        estado = NORMAL;
        contandoPersistencia = false;
        emitirEvento("imobilidade.decisao", "REARMADO_MANUAL", magnitude);
      }
      break;
    }
  }
}
