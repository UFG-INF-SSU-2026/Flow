package com.example.sensorapp

import java.util.Date
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** Validacao da CA importada pela pessoa: so um ca.crt valido e aceito (certificados de teste, sem segredo). */
class CaProjetoTest {

    // CA de teste (CA:TRUE), valida por ~100 anos
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
""".trimIndent().toByteArray()

    // Certificado de servidor de teste (CA:FALSE), assinado pela CA acima
    private val servidor = """
-----BEGIN CERTIFICATE-----
MIIDHDCCAgSgAwIBAgIULdraP8+uBJePg0kc9TCbLfUZJI8wDQYJKoZIhvcNAQEL
BQAwGDEWMBQGA1UEAwwNZmxvdy1jYS10ZXN0ZTAgFw0yNjExMjMxMjM0MjBaGA8y
MTI2MTAzMDEyMzQyMFowFjEUMBIGA1UEAwwLYm9yZGEtdGVzdGUwggEiMA0GCSqG
SIb3DQEBAQUAA4IBDwAwggEKAoIBAQCuJXIllSsxl2jirXrj73Z/RpI1ZqFkcOaZ
iwn0kfesd0yjcMagNWBWCrLTuSzS5rdf/mJQimFyl2oJ5Si3ge/wYE7/hinODOW/
94I1hUMXjHLg0ofB2igp7gkiCKykx3TQsdXjSNzs+Wh6KCxv+c0sGXZWoHDF11KB
f4Yw8Oy/A7LYWSe8mkGO+m5yglMwFVF2czUIxxwUZKiuq7dg8DWlDb8nDduBXIls
MvLmueKjivjQ9/rPqjBlRlvEPR49Bg41K/Okyj0Ew0xBT8huM3wAFlwmeO5AGCH5
2cbzjiosTmh7gvNy3+oSFajFJkz6QZbhc0Nym1ObMmoI6UGuo6jRAgMBAAGjXjBc
MAkGA1UdEwQCMAAwDwYDVR0RBAgwBocEfwAAATAdBgNVHQ4EFgQUvwMiLDfUUAmZ
Z7jm11Frm6BZlwAwHwYDVR0jBBgwFoAU6FLIb21mFkXVM0FiCWbg54gu5EIwDQYJ
KoZIhvcNAQELBQADggEBACSL/I2GIl6ciWAxcieVVltuecR3dw9CmpeJcGDs7/eE
hD8yEYNwMfNgTPjBbXNlufh0BoUZoHxHg9wXTdpvjvTRrGydidvaDYg17Rc7E/rS
Ucgm2fyXW67oHt7P0Us+yc7iscvM+9twvlMrbmBy+SUwIwdcqIDX0whxOcpC6C4i
lp/y70buTSz2qXBZpquu4AQGtzWbpdClWucWx4ofxcpGOPmrOX/7bJ+hiThaBSw3
0Jo+OQguiXyS4ois0lgKQ1efU+SwfdZMQ9L5f8N1IKHK3CZVPChTTX98mVRgOLf1
2LPy048hAzUTsFCZIP4Zj/BcFYkLPQFWbPLLRo7RKzU=
-----END CERTIFICATE-----
""".trimIndent().toByteArray()

    private fun erro(r: ResultadoCa): String = (r as ResultadoCa.Erro).motivo

    @Test
    fun aceitaACaValida() {
        val r = interpretarCa(ca)
        assertTrue(r is ResultadoCa.Ok)
        assertEquals(1, (r as ResultadoCa.Ok).certificados.size)
    }

    @Test
    fun recusaOCertificadoDeServidor() {
        assertTrue(erro(interpretarCa(servidor)).contains("não é uma CA"))
    }

    @Test
    fun recusaCaVencidaOuAindaNaoValida() {
        assertTrue(erro(interpretarCa(ca, Date(0))).contains("vencido ou ainda não válido"))
    }

    @Test
    fun recusaArquivoComChavePrivadaSemLerOCertificado() {
        val comChave = ca + "\n-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n".toByteArray()
        assertTrue(erro(interpretarCa(comChave)).contains("chave privada"))
    }

    @Test
    fun recusaVazioLixoEArquivoGrande() {
        assertEquals("arquivo vazio", erro(interpretarCa(ByteArray(0))))
        assertTrue(erro(interpretarCa("isto nao e um certificado".toByteArray())).contains("X.509"))
        assertTrue(erro(interpretarCa(ByteArray(LIMITE_BYTES + 1) { 'A'.code.toByte() })).contains("grande demais"))
    }

    @Test
    fun impressaoDigitalTemOitoBytesEmHexadecimal() {
        val c = (interpretarCa(ca) as ResultadoCa.Ok).certificados.first()
        assertTrue(Regex("([0-9A-F]{2}:){7}[0-9A-F]{2}").matches(impressaoDigital(c)))
    }

    @Test
    fun confiancaSemCaOuComCaNaoLanca() {
        CaProjeto.confianca(emptyList())
        val c = (interpretarCa(ca) as ResultadoCa.Ok).certificados
        val (_, gerente) = CaProjeto.confianca(c)
        assertEquals(1, gerente.acceptedIssuers.size)
    }

    @Test
    fun lerLimitadoParaDepoisDoLimite() {
        val lido = lerLimitado(ByteArray(1_000_000).inputStream(), 1000)
        assertTrue(lido.size in 1001..1000 + 4096)
    }
}
