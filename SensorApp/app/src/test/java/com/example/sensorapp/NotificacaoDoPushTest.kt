package com.example.sensorapp

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** notificacaoDoPush: mensagem de push ja validada -> o que fazer com a notificacao. Roda na JVM. */
class NotificacaoDoPushTest {

    @Test
    fun solicitadaViraNotificacaoComPrazoEAcao() {
        for (prazo in listOf(1L, 5_000L, 30_000L)) {
            val acao = notificacaoDoPush(MensagemPush.Solicitada("m-1", "ep-1", prazo))
            assertTrue(acao is AcaoNotificacao.Mostrar)
            val n = (acao as AcaoNotificacao.Mostrar).notificacao
            assertEquals("ep-1", n.episodeId)
            assertEquals(prazo, n.prazoMs)
            assertEquals("Estou bem", n.rotuloAcao)
            assertTrue(n.titulo.isNotEmpty() && n.texto.isNotEmpty())
        }
    }

    @Test
    fun prazoZeroOuNegativoNaoViraNotificacao() {
        for (prazo in listOf(0L, -5L)) {
            assertEquals(AcaoNotificacao.Ignorar, notificacaoDoPush(MensagemPush.Solicitada("m-1", "ep-1", prazo)))
        }
    }

    @Test
    fun encerradaCancelaANotificacaoDoEpisodio() {
        for (resultado in listOf("CONFIRMADO", "EXPIRADO", "CANCELADO")) {
            assertEquals(
                AcaoNotificacao.Cancelar("ep-1"),
                notificacaoDoPush(MensagemPush.Encerrada("m-2", "ep-1", resultado))
            )
        }
    }
}
