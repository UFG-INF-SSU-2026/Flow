package com.example.sensorapp

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.TimeZone

/** Leitura das respostas do cuidador, texto do estado e do historico, idade ("ha X") e leitor de datas ISO 8601. */
class CuidadorTest {

    private val utc = TimeZone.getTimeZone("UTC")

    private val episodioJson = """{"episodioId":"e1","deviceId":"d1","tipo":"queda","estado":"aguardando_confirmacao",
        "abertoEm":"2026-11-24T19:30:00+00:00","atualizadoEm":"2026-11-24T19:31:00+00:00","encerradoEm":null,"resultado":null}"""

    private fun estadoJson(episodio: String = episodioJson) = """{"schemaVersion":1,"userId":1,
        "geradoEm":"2026-11-24T19:40:00.123456+00:00",
        "comunicacoes":[{"deviceId":"d1","fonte":"app","instante":"2026-11-24T19:39:20+00:00"}],
        "episodioAberto":$episodio,
        "alertas":[{"instante":"2026-11-24T19:20:00+00:00","deviceId":"d1","tipo":"alerta_luz_inadequada","mensagem":"m","episodioId":null}]}"""

    @Test
    fun estadoValidoELido() {
        val e = lerEstado(estadoJson())!!
        assertEquals(1, e.userId)
        assertEquals("d1", e.comunicacoes[0].deviceId)
        assertEquals("queda", e.episodioAberto!!.tipo)
        assertNull(e.episodioAberto!!.encerradoEm)
        assertEquals(1, e.alertas.size)
        val linhas = linhasDoEstado(e, 0, utc)
        assertTrue(linhas.any { it.startsWith("Episódio aberto: Queda, aguardando confirmação") })
        assertTrue(linhas.contains("24/11 19:20  Alerta: luz inadequada"))
    }

    @Test
    fun estadoComEpisodioAbertoNulo() {
        val e = lerEstado(estadoJson("null"))!!
        assertNull(e.episodioAberto)
        assertTrue(linhasDoEstado(e, 0, utc).contains("Nenhum episódio aberto."))
    }

    @Test
    fun jsonInvalidoOuSemCampoObrigatorioDevolveNull() {
        assertNull(lerEstado("isto nao e json"))
        assertNull(lerEstado("{}"))
        assertNull(lerEstado(estadoJson().replace("\"episodioAberto\"", "\"outro\"")))
        assertNull(lerEstado(estadoJson().replace("\"userId\":1", "\"userId\":\"1\"")))
        assertNull(lerEstado(estadoJson().replace("\"schemaVersion\":1", "\"schemaVersion\":2")))
        assertNull(lerEstado(estadoJson().replace("\"geradoEm\":\"2026-11-24T19:40:00.123456+00:00\"", "\"geradoEm\":\"ontem\"")))
        assertNull(lerEstado(estadoJson(episodioJson.replace("\"estado\":\"aguardando_confirmacao\",", ""))))
        assertNull(lerHistorico("""{"schemaVersion":1,"userId":1,"geradoEm":"2026-11-24T19:40:00+00:00","alertas":[]}"""))
        assertNull(lerHistorico("""{"schemaVersion":1,"userId":1,"geradoEm":"2026-11-24T19:40:00+00:00",
            "alertas":[1],"confirmacoes":[],"episodios":[]}"""))
    }

    @Test
    fun historicoJuntaEOrdenaDoMaisRecenteParaOMaisAntigo() {
        val h = lerHistorico(
            """{"schemaVersion":1,"userId":1,"geradoEm":"2026-11-24T20:00:00+00:00",
            "alertas":[{"instante":"2026-11-24T19:00:00+00:00","deviceId":"d1","tipo":"alerta_luz_inadequada","mensagem":"m","episodioId":null}],
            "confirmacoes":[{"instante":"2026-11-24T19:45:00+00:00","tipo":"confirmacao.resposta","episodioId":"e1","estado":"ESTOU_BEM"}],
            "episodios":[{"episodioId":"e1","deviceId":"d1","tipo":"queda","estado":"encerrado","abertoEm":"2026-11-24T19:30:00+00:00",
              "atualizadoEm":"2026-11-24T19:50:00+00:00","encerradoEm":"2026-11-24T19:50:00+00:00","resultado":"EXPIRADO"},
              {"episodioId":"e0","deviceId":"d1","tipo":"queda","estado":"encerrado","abertoEm":"ilegivel",
              "atualizadoEm":null,"encerradoEm":null,"resultado":"CONFIRMADO"}]}"""
        )!!
        assertEquals(
            listOf(
                "24/11 19:45  Confirmação: Estou bem",
                "24/11 19:30  Queda: expirado",
                "24/11 19:00  Alerta: luz inadequada",
                "Queda: confirmado"
            ),
            linhasDoHistorico(h, utc)
        )
    }

    @Test
    fun haNosLimites() {
        assertEquals("agora", ha(-1000))
        assertEquals("agora", ha(4999))
        assertEquals("há 5 s", ha(5000))
        assertEquals("há 40 s", ha(40_000))
        assertEquals("há 59 s", ha(59_999))
        assertEquals("há 1 min", ha(60_000))
        assertEquals("há 3 min", ha(3 * 60_000L))
        assertEquals("há 59 min", ha(3_599_999))
        assertEquals("há 1 h", ha(3_600_000))
        assertEquals("há 2 h", ha(2 * 3_600_000L))
        assertEquals("há 1 dias", ha(86_400_000))
        assertEquals("há 5 dias", ha(5 * 86_400_000L))
    }

    @Test
    fun lerIso8601ComESemFracaoEComTextoInvalido() {
        val base = 1795549166000L // 2026-11-24T19:39:26Z
        assertEquals(base, lerIso8601("2026-11-24T19:39:26+00:00"))
        assertEquals(base, lerIso8601("2026-11-24T19:39:26.123456+00:00"))
        assertEquals(base, lerIso8601("2026-11-24T16:39:26-03:00"))
        assertNull(lerIso8601("ontem"))
        assertNull(lerIso8601("2026-13-45T25:00:00+00:00"))
        assertNull(lerIso8601(""))
        assertNull(lerIso8601(null))
        assertNotNull(lerIso8601("2026-11-24T19:39:26.5+00:00"))
    }

    @Test
    fun ultimaComunicacaoUsaGeradoEmMenosInstanteMaisOTempoDecorrido() {
        val e = lerEstado(estadoJson())!! // geradoEm 19:40:00, instante 19:39:20 -> 40 s
        assertTrue(linhasDoEstado(e, 0, utc).contains("d1 (app): última comunicação há 40 s"))
        assertTrue(linhasDoEstado(e, 140_000, utc).contains("d1 (app): última comunicação há 3 min"))
    }
}
