package com.example.sensorapp

import org.junit.Assert.assertEquals
import org.junit.Test

/** Texto de falha com a idade do ultimo envio e titulo com a versao (a funcao ha() ja tem teste proprio em CuidadorTest). */
class TextosDaTelaTest {
    private val base = "Servidor indisponivel (tentando novamente)"

    @Test
    fun semSucessoAnteriorNaoMostraIdade() {
        assertEquals(base, textoDeFalhaDeEnvio(base, null))
    }

    @Test
    fun sucessoHaPoucosSegundos() {
        assertEquals("$base, último envio agora", textoDeFalhaDeEnvio(base, 3_000L))
        assertEquals("$base, último envio há 40 s", textoDeFalhaDeEnvio(base, 40_000L))
    }

    @Test
    fun sucessoHaMinutos() {
        assertEquals("$base, último envio há 3 min", textoDeFalhaDeEnvio(base, 180_000L))
    }

    @Test
    fun valorNegativoContaComoZero() {
        assertEquals("$base, último envio agora", textoDeFalhaDeEnvio(base, -5_000L))
    }

    @Test
    fun versaoMostraNomeESchemaJuntos() {
        assertEquals("Sensor App v4.0 · schema 3", textoDaVersao("4.0", 3))
    }

    @Test
    fun versaoSemNomeCaiNumTextoLegivel() {
        assertEquals("Sensor App · schema 3", textoDaVersao(null, 3))
        assertEquals("Sensor App · schema 3", textoDaVersao("", 3))
        assertEquals("Sensor App · schema 3", textoDaVersao("   ", 3))
    }
}
