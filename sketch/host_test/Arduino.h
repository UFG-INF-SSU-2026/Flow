#pragma once
#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <cmath>
#include <string>
#include <deque>
#include <vector>
#include <cstdio>
#define HIGH 1
#define LOW 0
#define INPUT_PULLUP 2
#define OUTPUT 3
static unsigned long g_millis = 0;
inline unsigned long millis() { return g_millis; }
inline void delay(unsigned long) {}
static int g_pin[64];
static int g_tone_pin = -1; static unsigned g_tone_freq = 0;
inline void pinMode(int, int) {}
inline void digitalWrite(int p, int v) { g_pin[p] = v; }
inline int digitalRead(int p) { return g_pin[p]; }
inline void tone(int pin, unsigned f) { g_tone_pin = pin; g_tone_freq = f; }
inline void noTone(int pin) { if (g_tone_pin == pin) { g_tone_pin = -1; g_tone_freq = 0; } }
struct SerialMock {
  std::string out; std::deque<char> in;
  void begin(int) {} operator bool() const { return true; }
  void print(const char* s) { out += s; }
  void print(int v) { out += std::to_string(v); }
  void print(unsigned long v) { out += std::to_string(v); }
  void print(uint32_t v) { out += std::to_string(v); }
  void print(float v, int d) { char b[32]; snprintf(b, 32, "%.*f", d, v); out += b; }
  void println(const char* s) { out += s; out += "\n"; }
  int available() { return (int)in.size(); }
  int read() { int c = in.front(); in.pop_front(); return c; }
  void feed(const std::string& s) { for (char c : s) in.push_back(c); }
};
static SerialMock Serial;
// Wire falso: o "MPU" devolve valores configuraveis; registra a escrita em ACCEL_CONFIG
struct WireMock {
  int16_t ax = 0, ay = 0, az = 0; int lastReg = -1; int accelCfg = -1; std::deque<uint8_t> rx; std::vector<int> txbuf;
  void begin() {}
  void beginTransmission(int) { txbuf.clear(); }
  size_t write(int v) { txbuf.push_back(v); return 1; }
  int endTransmission(bool = true) {
    if (txbuf.size() == 2 && txbuf[0] == 0x1C) accelCfg = txbuf[1];
    if (txbuf.size() == 1) lastReg = txbuf[0];
    return 0;
  }
  int requestFrom(int, int n) { rx.clear(); int16_t v[3] = {ax, ay, az}; for (int i = 0; i < 3; i++) { rx.push_back((v[i] >> 8) & 0xFF); rx.push_back(v[i] & 0xFF);} return n; }
  int read() { int c = rx.front(); rx.pop_front(); return c; }
};
static WireMock Wire;
inline char* strtok_r_(char* s, const char* d, char** c) { return strtok_r(s, d, c); }
