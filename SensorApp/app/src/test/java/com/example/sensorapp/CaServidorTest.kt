package com.example.sensorapp

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Importar a CA do servidor: URL do pedido, corpo, mensagens de falha e validacao da CA recebida (sem rede). */
class CaServidorTest {

    // CA de teste (CA:TRUE), a mesma de CaProjetoTest
    private val ca = """
-----BEGIN CERTIFICATE-----
MIIDJjCCAg6gAwIBAgIUUl69GqoVlGhF0ISERM7HXPCxMogwDQYJKoZIhvcNAQEL
BQAwGDEWMBQGA1UEAwwNZmxvdy1jYS10ZXN0ZTAgFw0yNjExMjMxMjM0MjBaGA8y
MTI2MTAzMDEyMzQyMFowGDEWMBQGA1UEAwwNZmxvdy1jYS10ZXN0ZTCCASIwDQYJ
KoZIhvcNAQEBBQADggEPADCCAQoCggEBALiAs3bNnJRM8hjumH8HOQFSv762lpIA
S5fgGgAOnID45/vTH7lEYdbnYmfGLPwbYBzBkltPyrDGCkD2FYEhtTRR3p84aQLy
Hi085KxTv4y8nHlzaFBn+Cw4vGxRut/oyUs2qeOm1ges+3kXeBIdTy42UWh9tXQT
Uw+7YDhEDLEgSWl02WxGtClyVpAvqK5aT6VdF/c32Q+CDvTP26odf799HBijssvT
qKltON/C3gH3wJdEKO8EB1df3c731poDWOwa+IFrE5XOwAgv4LEPOSwGwPCwluZY
x8m4bQUr28RPgqkSLCgNR/4hgAHNglhphomrZQWokvSeTIViiSzUmI8CAwEAAaNm
MGQwHQYDVR0OBBYEFOhSyG9tZhZF1TNBYglm4OeILuRCMB8GA1UdIwQYMBaAFOhS
yG9tZhZF1TNBYglm4OeILuRCMBIGA1UdEwEB/wQIMAYBAf8CAQAwDgYDVR0PAQH/
BAQDAgEGMA0GCSqGSIb3DQEBCwUAA4IBAQAgMzk5zj+UwJOJpE0EnJV3h0ronn6V
AvvGE8H2q68aW63PdBWjdfExrWPCV8sTVxPoTsMvtTSu9nOM0viqI4DatHv9lp+Y
DDuYGUIvcy+Xe52H1EQCGaVb6X37G7b2dKSuh8BJly2Y1MCpvUBAEI1CQ++EQUzM
UoekY0ihPjv+4j5mKJLVqB9eOS9AszqcGGIo+j2kYVrIsmnieCPDJR0MxCaCZsdi
iFCWYiTxYoo3LILR6vvdbgUyyOSH0SLocNSorqtyWdFunqlCxIPl6Ooaayv3C8TF
nfHcMDu5ZnAjS2/3IUw5CjTJTQ96KTSaijezwL9hYZ1Z9XWfyCrpQ1O1
-----END CERTIFICATE-----
""".trimIndent()

    @Test
    fun urlMontaHttpsComCaminhoDaRota() {
        assertEquals("https://192.168.0.10:5000/auth/ca", CaServidor.url("192.168.0.10", "5000").toString())
        assertEquals("https://192.168.0.10:5000/auth/ca", CaServidor.url(" 192.168.0.10 ", " 5000 ").toString())
    }

    @Test
    fun urlRecusaIpOuPortaInvalidos() {
        assertNull(CaServidor.url("", "5000"))
        assertNull(CaServidor.url("192.168.0.10", ""))
        assertNull(CaServidor.url("192.168.0.10", "abc"))
        assertNull(CaServidor.url("192.168.0.10", "0"))
        assertNull(CaServidor.url("192.168.0.10", "65536"))
        assertNull(CaServidor.url("ip com espaco/", "5000"))
    }

    @Test
    fun corpoDoPedidoTemCredenciaisEFonteApp() {
        val c = CaServidor.corpoDoPedido("idoso1", "senha")
        assertEquals(1, c.getInt("schemaVersion"))
        assertEquals("idoso1", c.getString("identificador"))
        assertEquals("senha", c.getString("segredo"))
        assertEquals("app", c.getString("fonte"))
    }

    @Test
    fun mensagensDeFalhaPorCodigoHttp() {
        assertTrue(CaServidor.erroDoCodigoHttp(401).contains("inválidos"))
        assertTrue(CaServidor.erroDoCodigoHttp(429).contains("Muitas tentativas"))
        assertTrue(CaServidor.erroDoCodigoHttp(404).contains("atualize"))
        assertTrue(CaServidor.erroDoCodigoHttp(503).contains("sem CA"))
        assertTrue(CaServidor.erroDoCodigoHttp(500).contains("HTTP 500"))
    }

    @Test
    fun respostaComCaValidaEAceita() {
        val r = CaServidor.interpretarResposta(JSONObject().put("ca", ca).toString())
        assertTrue(r is CaServidor.Resultado.Ok)
        val ok = r as CaServidor.Resultado.Ok
        assertEquals(1, ok.certificados.size)
        assertNotNull(ok.bytes)
    }

    @Test
    fun respostaSemCaOuComLixoERecusada() {
        assertTrue(CaServidor.interpretarResposta("{}") is CaServidor.Resultado.Erro)
        assertTrue(CaServidor.interpretarResposta("nao e json") is CaServidor.Resultado.Erro)
        assertTrue(CaServidor.interpretarResposta(JSONObject().put("ca", "lixo").toString()) is CaServidor.Resultado.Erro)
    }

    @Test
    fun respostaComChavePrivadaERecusada() {
        val r = CaServidor.interpretarResposta(JSONObject().put("ca", ca + "\n-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n").toString())
        assertTrue(r is CaServidor.Resultado.Erro)
    }
}
