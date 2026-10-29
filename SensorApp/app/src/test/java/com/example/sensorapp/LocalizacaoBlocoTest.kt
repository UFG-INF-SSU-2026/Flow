package com.example.sensorapp

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test

/** montarBlocoLocalizacao: o app so descarta o absurdo (o servidor valida). Roda na JVM. */
class LocalizacaoBlocoTest {
    private val agora = 1_800_000_000_000L

    private fun bloco(
        lat: Double = -16.68,
        lon: Double = -49.25,
        precisao: Double = 12.0,
        instante: Long = agora - 5_000L
    ) = montarBlocoLocalizacao(lat, lon, precisao, instante, agora)

    @Test
    fun posicaoBoaViraBlocoComTodosOsCampos() {
        val b = bloco()
        assertNotNull(b)
        assertEquals(SCHEMA_VERSION_LOCALIZACAO, b!!.getInt("schemaVersion"))
        assertEquals(-16.68, b.getDouble("latitude"), 0.0)
        assertEquals(-49.25, b.getDouble("longitude"), 0.0)
        assertEquals(12.0, b.getDouble("precisao_m"), 0.0)
        assertNotNull(b.getString("instante_medicao"))
    }

    @Test
    fun valoresAbsurdosOmitemOBloco() {
        assertNull(bloco(lat = 90.1))
        assertNull(bloco(lat = -91.0))
        assertNull(bloco(lon = 180.5))
        assertNull(bloco(lon = -181.0))
        assertNull(bloco(lat = Double.NaN))
        assertNull(bloco(lon = Double.POSITIVE_INFINITY))
        assertNull(bloco(precisao = -1.0))
        assertNull(bloco(precisao = Double.NaN))
    }

    @Test
    fun posicaoVelhaOuNoFuturoOmiteOBloco() {
        assertNull(bloco(instante = agora - IDADE_MAX_LOCALIZACAO_MS - 1))
        assertNull(bloco(instante = agora + FUTURO_MAX_LOCALIZACAO_MS + 1))
        assertNotNull(bloco(instante = agora - IDADE_MAX_LOCALIZACAO_MS))
    }
}
