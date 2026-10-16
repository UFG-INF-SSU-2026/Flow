package com.example.sensorapp

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * validarPush: a mensagem de push so chega ao AlertaController (thread principal) se a forma estiver correta.
 * Roda na JVM, sem emulador: no Android Studio, botao direito em app/src/test > Run Tests (ou `gradle test` em SensorApp).
 */
class MensagemPushTest {

    private fun solicitada(payload: String = """{"timeoutMs": 5000, "motivo": "IMPACTO_SEGUIDO_DE_IMOBILIDADE"}""") =
        """{"schemaVersion":3,"messageId":"m-1","messageType":"queda.confirmacao_solicitada","userId":1,
            "episodeId":"ep-1","payload":$payload}"""

    private fun encerrada(payload: String = """{"resultado": "CONFIRMADO"}""") =
        """{"schemaVersion":3,"messageId":"m-2","messageType":"queda.confirmacao_encerrada","userId":1,
            "episodeId":"ep-1","payload":$payload}"""

    private fun validar(json: String) = validarPush(JSONObject(json))

    // ------------------------------------------------------------------ mensagens validas
    @Test
    fun solicitadaValidaViraSolicitada() {
        assertEquals(MensagemPush.Solicitada("m-1", "ep-1", 5000L), validar(solicitada()))
    }

    @Test
    fun encerradaValidaViraEncerrada() {
        assertEquals(MensagemPush.Encerrada("m-2", "ep-1", "CONFIRMADO"), validar(encerrada()))
    }

    @Test
    fun timeoutZeroOuNegativoPassaParaOAppRejeitar() {
        // O valor nao e julgado aqui: AlertaController responde push.ack REJEITADO.
        assertEquals(0L, (validar(solicitada("""{"timeoutMs": 0}""")) as MensagemPush.Solicitada).timeoutMs)
        assertEquals(-5L, (validar(solicitada("""{"timeoutMs": -5}""")) as MensagemPush.Solicitada).timeoutMs)
    }

    @Test
    fun motivoAusenteNaoImporta() {
        assertNotNull(validar(solicitada("""{"timeoutMs": 5000}""")))
    }

    // ------------------------------------------------------------------ solicitada fora do formato
    @Test
    fun payloadAusenteNaoCausaExcecao() {
        val sem = """{"messageId":"m-1","messageType":"queda.confirmacao_solicitada","episodeId":"ep-1"}"""
        assertNull(validar(sem))
    }

    @Test
    fun payloadQueNaoEObjetoEDescartado() {
        assertNull(validar(solicitada("\"texto\"")))
        assertNull(validar(solicitada("[5000]")))
        assertNull(validar(solicitada("null")))
        assertNull(validar(solicitada("5000")))
    }

    @Test
    fun timeoutMsAusenteOuDoTipoErradoEDescartado() {
        assertNull(validar(solicitada("""{"motivo": "x"}""")))
        assertNull(validar(solicitada("""{"timeoutMs": "5000"}""")))
        assertNull(validar(solicitada("""{"timeoutMs": true}""")))
        assertNull(validar(solicitada("""{"timeoutMs": null}""")))
        assertNull(validar(solicitada("""{"timeoutMs": [5000]}""")))
    }

    // ------------------------------------------------------------------ encerrada fora do formato
    @Test
    fun resultadoAusenteOuDoTipoErradoEDescartado() {
        assertNull(validar(encerrada("""{}""")))
        assertNull(validar(encerrada("""{"resultado": 1}""")))
        assertNull(validar(encerrada("""{"resultado": null}""")))
        assertNull(validar(encerrada("null")))
    }

    // ------------------------------------------------------------------ campos comuns
    @Test
    fun messageTypeAusenteDesconhecidoOuNaoTextoEDescartado() {
        assertNull(validar("""{"messageId":"m-1","episodeId":"ep-1","payload":{"timeoutMs":5000}}"""))
        assertNull(validar("""{"messageType":"outro.tipo","messageId":"m-1","episodeId":"ep-1","payload":{}}"""))
        assertNull(validar("""{"messageType":7,"messageId":"m-1","episodeId":"ep-1","payload":{"timeoutMs":5000}}"""))
        assertNull(validar("""{"messageType":null,"messageId":"m-1","episodeId":"ep-1","payload":{"timeoutMs":5000}}"""))
    }

    @Test
    fun episodeIdAusenteOuVazioEDescartadoNosDoisTipos() {
        assertNull(validar("""{"messageId":"m-1","messageType":"queda.confirmacao_solicitada","payload":{"timeoutMs":5000}}"""))
        assertNull(validar("""{"messageId":"m-2","messageType":"queda.confirmacao_encerrada","payload":{"resultado":"EXPIRADO"}}"""))
        assertNull(validar(solicitada().replace("\"ep-1\"", "\"\"")))
        assertNull(validar(solicitada().replace("\"ep-1\"", "9")))
    }

    @Test
    fun messageIdAusenteOuVazioEDescartado() {
        assertNull(validar("""{"messageType":"queda.confirmacao_solicitada","episodeId":"ep-1","payload":{"timeoutMs":5000}}"""))
        assertNull(validar(solicitada().replace("\"m-1\"", "\"\"")))
        assertNull(validar(solicitada().replace("\"m-1\"", "42")))
    }

    @Test
    fun mensagemVaziaNaoCausaExcecao() {
        assertNull(validar("{}"))
    }
}
