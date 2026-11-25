package com.example.sensorapp

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** Qual usuario vale lembrar para o proximo login (a senha nunca entra nisso). */
class UsuarioLembradoTest {
    @Test
    fun usuarioNormalEGuardado() {
        assertEquals("idoso1", usuarioParaGuardar("idoso1"))
    }

    @Test
    fun espacosNasPontasSaoRemovidos() {
        assertEquals("idoso1", usuarioParaGuardar("  idoso1 "))
    }

    @Test
    fun vazioSoEspacosENuloNaoSaoGuardados() {
        assertNull(usuarioParaGuardar(""))
        assertNull(usuarioParaGuardar("   "))
        assertNull(usuarioParaGuardar(null))
    }
}
