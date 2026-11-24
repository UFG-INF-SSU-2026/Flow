#pragma once
// Envelope do protocolo: AES-256-GCM com a chave compartilhada (FLOW_CHAVE_AES_B64, em segredos.h), no mesmo formato que o
// servidor e o app: JSON {"nonce": "<base64>", "ciphertext": "<base64>"}, nonce de 12 bytes ALEATORIO por mensagem, tag de 16 bytes
// anexada ao fim do ciphertext, sem AAD, base64 padrao sem quebra de linha. Sem String do Arduino (compila tambem no PC).
//
// Falha de tag, base64 invalido, nonce de tamanho errado ou campo ausente: devolve erro e NUNCA texto parcial.
// A chave e fixa e compartilhada (limitacao assumida da arquitetura); nunca e impressa.
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "mbedtls/gcm.h"
#include "mbedtls/base64.h"
#include <ArduinoJson.h>
#include "segredos.h"          // FLOW_CHAVE_AES_B64

// Tamanhos fixos: o maior texto e a emissao do certificado (~1,3 KB); o telemetria tem ~250 bytes.
#define ENV_TEXTO_MAX   1536                                  // texto claro, sem o '\0'
#define ENV_JSON_MAX    (((ENV_TEXTO_MAX + 16 + 2) / 3) * 4 + 16 + 64)   // envelope: base64 do ciphertext, nonce e molde do JSON

#ifdef ARDUINO
#include <esp_random.h>
static inline void flowAleatorio(uint8_t* b, size_t n) { esp_fill_random(b, n); }
#else
// SO NO TESTE DO PC: gerador previsivel (nunca vai para o firmware). O teste pode fixar o nonce com g_aleatorio_fixo.
static const uint8_t* g_aleatorio_fixo = nullptr;
static uint32_t g_aleatorio_contador = 0;
static inline void flowAleatorio(uint8_t* b, size_t n) {
  for (size_t i = 0; i < n; i++) b[i] = g_aleatorio_fixo ? g_aleatorio_fixo[i % 12] : (uint8_t)(++g_aleatorio_contador * 37u);
}
#endif

static uint8_t g_aesChave[32];
static bool g_aesPronta = false;

// Decodifica a chave (base64) e exige 32 bytes. Falha com mensagem clara, sem imprimir a chave.
static bool envelopeIniciarCom(const char* chaveB64) {
  size_t olen = 0;
  int r = mbedtls_base64_decode(g_aesChave, sizeof(g_aesChave), &olen, (const unsigned char*)chaveB64, strlen(chaveB64));
  g_aesPronta = (r == 0 && olen == 32);
  if (!g_aesPronta) {
    memset(g_aesChave, 0, sizeof(g_aesChave));
    Serial.println("chave AES invalida (FLOW_CHAVE_AES_B64): precisa decodificar para 32 bytes");
  }
  return g_aesPronta;
}
static bool envelopeIniciar() { return envelopeIniciarCom(FLOW_CHAVE_AES_B64); }

// Cifra `texto` (n bytes) e escreve o JSON do envelope em `saida` (com '\0'). Devolve o tamanho, ou -1 em erro.
static int envelopeCifrar(const char* texto, size_t n, char* saida, size_t cap) {
  static uint8_t cifrado[ENV_TEXTO_MAX + 16];
  if (!g_aesPronta || n > ENV_TEXTO_MAX) return -1;
  uint8_t nonce[12];
  flowAleatorio(nonce, sizeof(nonce));                       // nunca derivado de millis()
  mbedtls_gcm_context gcm;
  mbedtls_gcm_init(&gcm);
  int r = mbedtls_gcm_setkey(&gcm, MBEDTLS_CIPHER_ID_AES, g_aesChave, 256);
  if (r == 0) r = mbedtls_gcm_crypt_and_tag(&gcm, MBEDTLS_GCM_ENCRYPT, n, nonce, sizeof(nonce), nullptr, 0,
                                            (const unsigned char*)texto, cifrado, 16, cifrado + n);
  mbedtls_gcm_free(&gcm);
  if (r != 0) return -1;
  char nonceB64[24], ctB64[((ENV_TEXTO_MAX + 16 + 2) / 3) * 4 + 4];
  size_t nl = 0, cl = 0;
  if (mbedtls_base64_encode((unsigned char*)nonceB64, sizeof(nonceB64) - 1, &nl, nonce, sizeof(nonce)) != 0) return -1;
  if (mbedtls_base64_encode((unsigned char*)ctB64, sizeof(ctB64) - 1, &cl, cifrado, n + 16) != 0) return -1;
  nonceB64[nl] = '\0';
  ctB64[cl] = '\0';
  int total = snprintf(saida, cap, "{\"nonce\":\"%s\",\"ciphertext\":\"%s\"}", nonceB64, ctB64);
  return (total > 0 && (size_t)total < cap) ? total : -1;
}

// Decifra o envelope (JSON de n bytes) para `saida` (com '\0'). Devolve o tamanho do texto, ou -1 em qualquer falha.
static int envelopeDecifrar(const char* envelope, size_t n, char* saida, size_t cap) {
  static uint8_t cifrado[ENV_TEXTO_MAX + 16];
  if (!g_aesPronta) return -1;
  JsonDocument doc;
  if (deserializeJson(doc, envelope, n) != DeserializationError::Ok) return -1;
  const char* nonceB64 = doc["nonce"].is<const char*>() ? doc["nonce"].as<const char*>() : nullptr;
  const char* ctB64 = doc["ciphertext"].is<const char*>() ? doc["ciphertext"].as<const char*>() : nullptr;
  if (!nonceB64 || !ctB64) return -1;
  uint8_t nonce[16];
  size_t nl = 0, cl = 0;
  if (mbedtls_base64_decode(nonce, sizeof(nonce), &nl, (const unsigned char*)nonceB64, strlen(nonceB64)) != 0 || nl != 12) return -1;
  if (mbedtls_base64_decode(cifrado, sizeof(cifrado), &cl, (const unsigned char*)ctB64, strlen(ctB64)) != 0 || cl < 16) return -1;
  size_t texto = cl - 16;
  if (texto + 1 > cap) return -1;
  mbedtls_gcm_context gcm;
  mbedtls_gcm_init(&gcm);
  int r = mbedtls_gcm_setkey(&gcm, MBEDTLS_CIPHER_ID_AES, g_aesChave, 256);
  if (r == 0) r = mbedtls_gcm_auth_decrypt(&gcm, texto, nonce, 12, nullptr, 0, cifrado + texto, 16, cifrado,
                                           (unsigned char*)saida);
  mbedtls_gcm_free(&gcm);
  if (r != 0) { memset(saida, 0, texto + 1 <= cap ? texto + 1 : cap); return -1; }          // nada parcial
  saida[texto] = '\0';
  return (int)texto;
}
