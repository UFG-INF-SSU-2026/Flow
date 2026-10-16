package com.example.sensorapp

import org.json.JSONObject

const val TIPO_CONFIRMACAO_SOLICITADA = "queda.confirmacao_solicitada"
const val TIPO_CONFIRMACAO_ENCERRADA = "queda.confirmacao_encerrada"

/**
 * Mensagem de push (docs/schemas.md, secao 6) cuja FORMA ja foi conferida por [validarPush].
 *
 * Quem recebe um [MensagemPush] nao precisa mais lidar com JSON: nao ha getString/getJSONObject que possa lancar
 * excecao na thread principal.
 */
sealed class MensagemPush {
    abstract val messageId: String
    abstract val episodeId: String

    /** "queda.confirmacao_solicitada": [timeoutMs] e RELATIVO (o app nao tem constante de timeout). */
    data class Solicitada(
        override val messageId: String,
        override val episodeId: String,
        val timeoutMs: Long
    ) : MensagemPush()

    /** "queda.confirmacao_encerrada": silencia o episodio [episodeId]. [resultado]: CONFIRMADO, EXPIRADO ou CANCELADO. */
    data class Encerrada(
        override val messageId: String,
        override val episodeId: String,
        val resultado: String
    ) : MensagemPush()
}

/**
 * Confere a FORMA de uma mensagem de push ja decifrada e autenticada, sem nunca lancar excecao.
 * Devolve null (a mensagem deve ser descartada em silencio) se:
 *  - messageType, messageId ou episodeId nao sao texto, ou messageId/episodeId estao vazios;
 *  - o messageType nao e um dos dois conhecidos;
 *  - payload nao e objeto;
 *  - "solicitada" sem payload.timeoutMs numerico (texto, booleano e null nao valem);
 *  - "encerrada" sem payload.resultado em texto.
 *
 * Nao julga o VALOR do timeoutMs: 0 ou negativo passa e o AlertaController responde push.ack REJEITADO.
 * Usa opt() + tipo explicito de proposito: optString/optLong convertem outros tipos em vez de recusar.
 */
fun validarPush(msg: JSONObject): MensagemPush? {
    val tipo = msg.opt("messageType") as? String ?: return null
    val messageId = (msg.opt("messageId") as? String)?.takeIf { it.isNotEmpty() } ?: return null
    val episodeId = (msg.opt("episodeId") as? String)?.takeIf { it.isNotEmpty() } ?: return null
    val payload = msg.opt("payload") as? JSONObject ?: return null
    return when (tipo) {
        TIPO_CONFIRMACAO_SOLICITADA -> {
            val timeoutMs = payload.opt("timeoutMs") as? Number ?: return null
            MensagemPush.Solicitada(messageId, episodeId, timeoutMs.toLong())
        }
        TIPO_CONFIRMACAO_ENCERRADA -> {
            val resultado = payload.opt("resultado") as? String ?: return null
            MensagemPush.Encerrada(messageId, episodeId, resultado)
        }
        else -> null
    }
}
