package com.example.sensorapp

import java.security.KeyPairGenerator
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.util.Base64
import java.util.Date
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** CSR montado a mao: estrutura, assinatura ECDSA-SHA256 conferida com a chave publica e PEM. */
class CsrDispositivoTest {

    private val par = KeyPairGenerator.getInstance("EC").apply { initialize(ECGenParameterSpec("secp256r1")) }.generateKeyPair()

    private fun assinar(dados: ByteArray): ByteArray =
        Signature.getInstance("SHA256withECDSA").run { initSign(par.private); update(dados); sign() }

    @Test
    fun csrTemAInfoNoInicioEAAssinaturaValidaNoFim() {
        var assinatura = ByteArray(0)
        val csr = montarCsr(par.public) { info -> assinar(info).also { assinatura = it } }
        val info = montarInfoCsr(par.public)
        assertEquals(0x30, csr[0].toInt())
        // A info vem logo depois do cabecalho da SEQUENCE externa (2 a 4 bytes)
        val inicio = (2..4).firstOrNull { csr.copyOfRange(it, it + info.size).contentEquals(info) }
        assertTrue(inicio != null)
        assertTrue(csr.copyOfRange(csr.size - assinatura.size, csr.size).contentEquals(assinatura))
        val v = Signature.getInstance("SHA256withECDSA").apply { initVerify(par.public); update(info) }
        assertTrue(v.verify(assinatura))
    }

    @Test
    fun csrCarregaAChavePublicaEOidEcdsaSha256() {
        val csr = montarCsr(par.public) { assinar(it) }
        val oid = byteArrayOf(0x2A, 0x86.toByte(), 0x48, 0xCE.toByte(), 0x3D, 0x04, 0x03, 0x02)
        fun contem(a: ByteArray, b: ByteArray) = (0..a.size - b.size).any { i -> b.indices.all { a[i + it] == b[it] } }
        assertTrue(contem(csr, par.public.encoded))
        assertTrue(contem(csr, oid))
    }

    @Test
    fun pemTemCabecalhoERodapeELinhasDe64() {
        val pem = csrEmPem(Base64.getEncoder().encodeToString(ByteArray(200) { it.toByte() }))
        val linhas = pem.trim().lines()
        assertEquals("-----BEGIN CERTIFICATE REQUEST-----", linhas.first())
        assertEquals("-----END CERTIFICATE REQUEST-----", linhas.last())
        assertTrue(linhas.drop(1).dropLast(1).dropLast(1).all { it.length == 64 })
    }
}
