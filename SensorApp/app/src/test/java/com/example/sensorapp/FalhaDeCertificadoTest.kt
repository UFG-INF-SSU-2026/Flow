package com.example.sensorapp

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
    fun handshakeRecusadoPeloServidorPedeCertificadoNovo() {
        assertTrue(falhaTlsPorCertificadoDeCliente(SSLHandshakeException("Received fatal alert: bad_certificate")))
    }

    @Test
    fun falhaNaVerificacaoDoServidorNuncaPedeCertificado() {
        assertFalse(falhaTlsPorCertificadoDeCliente(SSLHandshakeException("x").apply { initCause(CertificateException("cadeia")) }))
        assertFalse(falhaTlsPorCertificadoDeCliente(SSLPeerUnverifiedException("nome")))
        assertFalse(falhaTlsPorCertificadoDeCliente(java.io.IOException("rede")))
    }
}
