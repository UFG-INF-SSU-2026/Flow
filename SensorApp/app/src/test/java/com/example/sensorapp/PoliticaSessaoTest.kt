package com.example.sensorapp

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Refresh: so 400, 401 e 403 encerram a sessao; o servidor ou o banco fora (503) nao desloga a pessoa. */
class PoliticaSessaoTest {
    @Test
    fun refreshRecusadoEncerraASessao() {
        for (codigo in listOf(400, 401, 403)) assertTrue("codigo $codigo", refreshEncerraSessao(codigo))
    }

    @Test
    fun falhaDoServidorMantemASessao() {
        for (codigo in listOf(500, 502, 503, 504, 429)) assertFalse("codigo $codigo", refreshEncerraSessao(codigo))
    }
}
