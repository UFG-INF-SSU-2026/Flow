package com.example.sensorapp

import com.example.sensorapp.CaProjeto.Origem
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Login em um passo so: quando baixar a CA do servidor antes do login (sem Android, sem rede). */
class DecisaoCaTest {
    private val alvo = "192.168.0.10:5000"

    @Test
    fun semCaBaixaSempre() {
        assertTrue(precisaBaixarCa(Origem.NENHUMA, alvo, null))
        assertTrue(precisaBaixarCa(Origem.NENHUMA, alvo, alvo))
        assertTrue(precisaBaixarCa(Origem.NENHUMA, alvo, "10.0.0.1:5000"))
    }

    @Test
    fun comCaENadaConfirmadoTentaOLoginDireto() {
        assertFalse(precisaBaixarCa(Origem.IMPORTADA, alvo, null))
        assertFalse(precisaBaixarCa(Origem.EMBUTIDA, alvo, null))
    }

    @Test
    fun comCaDoMesmoServidorNaoBaixa() {
        assertFalse(precisaBaixarCa(Origem.IMPORTADA, alvo, alvo))
        assertFalse(precisaBaixarCa(Origem.EMBUTIDA, alvo, alvo))
    }

    @Test
    fun outroIpOuOutraPortaBaixaDeNovo() {
        assertTrue(precisaBaixarCa(Origem.IMPORTADA, "10.0.0.1:5000", alvo))
        assertTrue(precisaBaixarCa(Origem.EMBUTIDA, "192.168.0.10:5001", alvo))
    }

    @Test
    fun destinoIgnoraEspacosNasPontas() {
        assertEquals("192.168.0.10:5000", destinoDoServidor(" 192.168.0.10 ", " 5000 "))
    }

    @Test
    fun repeteOLoginSoComFalhaDeTlsECaNaoBaixadaAgora() {
        assertTrue(repetirLoginComCaNova(falhaDeTls = true, caBaixadaNestaExecucao = false))
        assertFalse(repetirLoginComCaNova(falhaDeTls = true, caBaixadaNestaExecucao = true))     // ja era a CA nova
        assertFalse(repetirLoginComCaNova(falhaDeTls = false, caBaixadaNestaExecucao = false))   // senha errada, rede fora...
        assertFalse(repetirLoginComCaNova(falhaDeTls = false, caBaixadaNestaExecucao = true))
    }
}
