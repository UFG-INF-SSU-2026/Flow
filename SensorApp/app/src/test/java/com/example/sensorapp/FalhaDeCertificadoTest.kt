package com.example.sensorapp

import java.io.EOFException
import java.net.ConnectException
import java.net.SocketException
import java.net.SocketTimeoutException
import java.net.UnknownHostException
import java.security.cert.CertificateException
import javax.net.ssl.SSLHandshakeException
import javax.net.ssl.SSLPeerUnverifiedException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Como o app separa "meu certificado foi recusado" (pedir outro) de "nao confio no servidor" (nunca relaxar). */
class FalhaDeCertificadoTest {

    @Test
    fun reconheceOsCodigosDeCertificadoDaBorda() {
        assertEquals("certificado_ausente", erroDeCertificadoNoCorpo(401, """{"status": "erro", "codigo": "certificado_ausente"}"""))
        assertEquals("certificado_incompativel", erroDeCertificadoNoCorpo(403, """{"codigo":"certificado_incompativel"}"""))
    }

    @Test
    fun outros401e403NaoSaoDeCertificado() {
        assertNull(erroDeCertificadoNoCorpo(401, """{"codigo": "token_expirado"}"""))
        assertNull(erroDeCertificadoNoCorpo(403, """{"codigo": "sem_permissao"}"""))
        assertNull(erroDeCertificadoNoCorpo(401, """{"codigo": "certificado_incompativel"}"""))   // codigo e status nao combinam
    }

    @Test
    fun recusaDoCertificadoPeloServidorPedeCertificadoNovo() {
        assertTrue(falhaDeConexaoComCertificado(SSLHandshakeException("Received fatal alert: bad_certificate")))
        // TLS 1.3: a recusa chega depois do handshake, como conexao fechada
        assertTrue(falhaDeConexaoComCertificado(SocketException("Broken pipe")))
        assertTrue(falhaDeConexaoComCertificado(SocketException("Connection reset")))
        assertTrue(falhaDeConexaoComCertificado(EOFException("fim do fluxo")))
    }

    @Test
    fun falhaNaVerificacaoDoServidorNuncaPedeCertificado() {
        assertFalse(falhaDeConexaoComCertificado(SSLHandshakeException("x").apply { initCause(CertificateException("cadeia")) }))
        assertFalse(falhaDeConexaoComCertificado(SSLPeerUnverifiedException("nome")))
    }

    @Test
    fun falhaSemConexaoOuSemRespostaNaoPedeCertificado() {
        assertFalse(falhaDeConexaoComCertificado(UnknownHostException("sem rede")))
        assertFalse(falhaDeConexaoComCertificado(ConnectException("recusada")))
        assertFalse(falhaDeConexaoComCertificado(SocketTimeoutException("tempo")))
    }

    @Test
    fun oQueNaoEFalhaDeConexaoNaoPedeCertificado() {
        assertFalse(falhaDeConexaoComCertificado(IllegalStateException("bug")))
    }

    // ---- login: CA guardada nao valida mais o servidor ----

    @Test
    fun falhaDeTlsNoServidorReconheceSslException() {
        assertTrue(falhaDeTlsNoServidor(SSLHandshakeException("handshake")))
        assertTrue(falhaDeTlsNoServidor(SSLPeerUnverifiedException("nome")))
        assertTrue(falhaDeTlsNoServidor(SSLHandshakeException("cadeia").initCause(CertificateException("nao confiavel"))))
    }

    @Test
    fun falhaDeTlsNoServidorAchaASslExceptionNaCausa() {
        assertTrue(falhaDeTlsNoServidor(java.io.IOException("envolvida", SSLHandshakeException("handshake"))))
    }

    @Test
    fun redeForaOuTempoEsgotadoNaoEFalhaDeTls() {
        assertFalse(falhaDeTlsNoServidor(UnknownHostException("sem rede")))
        assertFalse(falhaDeTlsNoServidor(ConnectException("recusada")))
        assertFalse(falhaDeTlsNoServidor(SocketTimeoutException("tempo")))
        assertFalse(falhaDeTlsNoServidor(IllegalStateException("bug")))
    }
}
