package com.example.sensorapp

import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/** Agregador de atuadores: liga e desliga todos, na ordem, e a falha de um nao impede os demais (atuadores falsos). */
class AtuadoresTest {

    private class Falso(val nome: String, val registro: MutableList<String>, val falhaAoLigar: Boolean = false, val falhaAoDesligar: Boolean = false) : Atuador {
        override fun ligar() {
            registro.add("ligar $nome")
            if (falhaAoLigar) throw IllegalStateException("sem $nome")
        }

        override fun desligar() {
            registro.add("desligar $nome")
            if (falhaAoDesligar) throw IllegalStateException("sem $nome")
        }
    }

    @Test
    fun ligarChamaTodosNaOrdemDaLista() {
        val r = mutableListOf<String>()
        Atuadores(listOf(Falso("a", r), Falso("b", r), Falso("c", r))).ligar()
        assertEquals(listOf("ligar a", "ligar b", "ligar c"), r)
    }

    @Test
    fun desligarChamaTodosNaOrdemDaLista() {
        val r = mutableListOf<String>()
        Atuadores(listOf(Falso("a", r), Falso("b", r), Falso("c", r))).desligar()
        assertEquals(listOf("desligar a", "desligar b", "desligar c"), r)
    }

    @Test
    fun falhaAoLigarNaoImpedeOsSeguintesEChegaAoAvisoDeFalha() {
        val r = mutableListOf<String>()
        val ruim = Falso("b", r, falhaAoLigar = true)
        val falhas = mutableListOf<Pair<Atuador, Exception>>()
        Atuadores(listOf(Falso("a", r), ruim, Falso("c", r))) { atuador, e -> falhas.add(atuador to e) }.ligar()
        assertEquals(listOf("ligar a", "ligar b", "ligar c"), r)
        assertEquals(1, falhas.size)
        assertSame(ruim, falhas[0].first)
        assertTrue(falhas[0].second is IllegalStateException)
    }

    @Test
    fun falhaAoDesligarNaoImpedeOsSeguintesEChegaAoAvisoDeFalha() {
        val r = mutableListOf<String>()
        val ruim = Falso("b", r, falhaAoDesligar = true)
        val falhas = mutableListOf<Pair<Atuador, Exception>>()
        Atuadores(listOf(Falso("a", r), ruim, Falso("c", r))) { atuador, e -> falhas.add(atuador to e) }.desligar()
        assertEquals(listOf("desligar a", "desligar b", "desligar c"), r)
        assertEquals(1, falhas.size)
        assertSame(ruim, falhas[0].first)
    }

    @Test
    fun desligarSemLigarAntesNaoLancaEChamaTodos() {
        val r = mutableListOf<String>()
        Atuadores(listOf(Falso("a", r), Falso("b", r))).desligar()
        assertEquals(listOf("desligar a", "desligar b"), r)
    }

    @Test
    fun desligarDuasVezesSeguidasNaoLanca() {
        val r = mutableListOf<String>()
        val a = Atuadores(listOf(Falso("a", r)))
        a.desligar()
        a.desligar()
        assertEquals(listOf("desligar a", "desligar a"), r)
    }

    @Test
    fun listaVaziaNaoFazNadaENaoLanca() {
        val a = Atuadores(emptyList())
        a.ligar()
        a.desligar()
    }

    @Test
    fun semAvisoDeFalhaAFalhaTambemNaoPropaga() {
        val r = mutableListOf<String>()
        val a = Atuadores(listOf(Falso("a", r, falhaAoLigar = true, falhaAoDesligar = true), Falso("b", r)))
        a.ligar()
        a.desligar()
        assertEquals(listOf("ligar a", "ligar b", "desligar a", "desligar b"), r)
    }

    @Test
    fun falhaNoProprioAvisoDeFalhaNaoImpedeOsDemais() {
        val r = mutableListOf<String>()
        Atuadores(listOf(Falso("a", r, falhaAoLigar = true), Falso("b", r))) { _, _ -> throw RuntimeException("log") }.ligar()
        assertEquals(listOf("ligar a", "ligar b"), r)
    }
}
