#pragma once
// Chave EC P-256 e CSR gerados NO PROPRIO dispositivo (mbedTLS do core). A chave privada nasce e fica so na RAM (a flash do
// simulador nao persiste e, em hardware real, o firmware tambem nao a grava); nunca vai para a Serial, para log nem para o
// corpo de requisicao nenhum. O servidor recebe so o CSR (POST /auth/certificado) e devolve o certificado.
//
// Gera-se UMA vez por partida (custa ~3 s de CPU no ESP32 simulado e ~32 KB de pico de heap), depois do login e antes de abrir
// as sessoes TLS com certificado. Os buffers abaixo vivem enquanto os clientes TLS existirem: o core guarda so o ponteiro do
// PEM de certificado e de chave.
#include <stdio.h>
#include <string.h>
#include "mbedtls/pk.h"
#include "mbedtls/ecp.h"
#include "mbedtls/entropy.h"
#include "mbedtls/ctr_drbg.h"
#include "mbedtls/x509_csr.h"

static char g_chavePem[320];       // chave privada em PEM (~230 bytes)
static char g_csrPem[512];         // CSR em PEM (~380 bytes)
static bool g_chaveGerada = false;

// Gera chave e CSR (CN = cn). Devolve false em qualquer falha (log curto, sem dados); a chave so e usada se tudo der certo.
static bool certificadoGerar(const char* cn) {
  if (g_chaveGerada) return true;                            // uma vez por partida
  mbedtls_entropy_context entropia;
  mbedtls_ctr_drbg_context drbg;
  mbedtls_pk_context pk;
  mbedtls_x509write_csr csr;
  mbedtls_entropy_init(&entropia);
  mbedtls_ctr_drbg_init(&drbg);
  mbedtls_pk_init(&pk);
  mbedtls_x509write_csr_init(&csr);

  static const char personalizacao[] = "flow-csr";
  int r = mbedtls_ctr_drbg_seed(&drbg, mbedtls_entropy_func, &entropia, (const unsigned char*)personalizacao, sizeof(personalizacao) - 1);
  if (r == 0) r = mbedtls_pk_setup(&pk, mbedtls_pk_info_from_type(MBEDTLS_PK_ECKEY));
  if (r == 0) r = mbedtls_ecp_gen_key(MBEDTLS_ECP_DP_SECP256R1, mbedtls_pk_ec(pk), mbedtls_ctr_drbg_random, &drbg);
  if (r == 0) r = mbedtls_pk_write_key_pem(&pk, (unsigned char*)g_chavePem, sizeof(g_chavePem));
  if (r == 0) {
    char assunto[96];
    snprintf(assunto, sizeof(assunto), "CN=%s", cn);
    mbedtls_x509write_csr_set_md_alg(&csr, MBEDTLS_MD_SHA256);       // ECDSA com SHA-256
    mbedtls_x509write_csr_set_key(&csr, &pk);
    r = mbedtls_x509write_csr_set_subject_name(&csr, assunto);
  }
  if (r == 0) r = mbedtls_x509write_csr_pem(&csr, (unsigned char*)g_csrPem, sizeof(g_csrPem), mbedtls_ctr_drbg_random, &drbg);

  mbedtls_x509write_csr_free(&csr);
  mbedtls_pk_free(&pk);
  mbedtls_ctr_drbg_free(&drbg);
  mbedtls_entropy_free(&entropia);
  if (r != 0) {
    memset(g_chavePem, 0, sizeof(g_chavePem));
    memset(g_csrPem, 0, sizeof(g_csrPem));
    Serial.println("certificado: falha ao gerar a chave/CSR");
    return false;
  }
  g_chaveGerada = true;
  return true;
}
