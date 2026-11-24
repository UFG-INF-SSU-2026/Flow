package com.example.sensorapp

import java.io.ByteArrayOutputStream
import java.security.PublicKey
import java.security.cert.X509Certificate
import java.util.Date

/*
 * Pedido de certificado (CSR PKCS#10) do dispositivo, montado a mao em DER: EC P-256 assinado com ECDSA e SHA-256, o
 * que a borda aceita. Evita uma biblioteca so para isto. O servidor ignora o assunto do CSR (usa a conta e o device_id
 * do token), por isso ele e fixo. Sem android.*: roda na JVM (ver CsrDispositivoTest).
 */

private fun der(tag: Int, conteudo: ByteArray): ByteArray {
    val saida = ByteArrayOutputStream()
    saida.write(tag)
    val n = conteudo.size
    when {
        n < 0x80 -> saida.write(n)
        n < 0x100 -> { saida.write(0x81); saida.write(n) }
        else -> { saida.write(0x82); saida.write(n shr 8); saida.write(n and 0xFF) }
    }
    saida.write(conteudo)
    return saida.toByteArray()
}

private fun juntar(vararg partes: ByteArray): ByteArray {
    val saida = ByteArrayOutputStream()
    partes.forEach { saida.write(it) }
    return saida.toByteArray()
}

/** Parte do CSR que e assinada (CertificationRequestInfo). [chavePublica].encoded e o SubjectPublicKeyInfo em DER. */
fun montarInfoCsr(chavePublica: PublicKey): ByteArray {
    val versao = byteArrayOf(0x02, 0x01, 0x00)                                    // INTEGER 0
    val oidCn = byteArrayOf(0x06, 0x03, 0x55, 0x04, 0x03)                         // 2.5.4.3 (commonName)
    val cn = der(0x0C, "dispositivo".toByteArray(Charsets.UTF_8))                 // UTF8String
    val assunto = der(0x30, der(0x31, der(0x30, juntar(oidCn, cn))))              // Name: SEQ { SET { SEQ { oid, valor } } }
    val atributos = byteArrayOf(0xA0.toByte(), 0x00)                              // [0] vazio
    return der(0x30, juntar(versao, assunto, chavePublica.encoded, atributos))
}

/**
 * CSR completo em DER. [assinar] recebe a parte a assinar e devolve a assinatura ECDSA-SHA256 no formato DER (o que
 * Signature "SHA256withECDSA" produz), com a chave privada que fica no aparelho.
 */
fun montarCsr(chavePublica: PublicKey, assinar: (ByteArray) -> ByteArray): ByteArray {
    val info = montarInfoCsr(chavePublica)
    val oidEcdsaSha256 = byteArrayOf(0x06, 0x08, 0x2A, 0x86.toByte(), 0x48, 0xCE.toByte(), 0x3D, 0x04, 0x03, 0x02)
    val algoritmo = der(0x30, oidEcdsaSha256)
    val assinatura = der(0x03, juntar(byteArrayOf(0x00), assinar(info)))         // BIT STRING (0 bits sobrando)
    return der(0x30, juntar(info, algoritmo, assinatura))
}

/** PEM do CSR a partir do DER em base64 sem quebras (a codificacao fica com quem chama: android.util.Base64 no app). */
fun csrEmPem(derEmBase64: String): String =
    "-----BEGIN CERTIFICATE REQUEST-----\n" +
        derEmBase64.chunked(64).joinToString("\n") +
        "\n-----END CERTIFICATE REQUEST-----\n"

/** Certificado vencido, ainda nao valido ou com menos da metade da validade restante: hora de emitir outro. */
fun precisaRenovar(certificado: X509Certificate, agora: Date = Date()): Boolean {
    val inicio = certificado.notBefore.time
    val fim = certificado.notAfter.time
    return agora.time < inicio || agora.time >= inicio + (fim - inicio) / 2
}
