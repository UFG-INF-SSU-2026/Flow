package com.example.sensorapp

/** Conteudo da notificacao do pedido de confirmacao de queda. */
data class NotificacaoPush(
    val episodeId: String,
    val titulo: String,
    val texto: String,
    /** Prazo RELATIVO (vem do servidor); vira setTimeoutAfter. */
    val prazoMs: Long,
    /** Rotulo da acao que responde o episodio direto da notificacao. */
    val rotuloAcao: String
)

/** O que uma mensagem de push pede que se faca com a notificacao. */
sealed class AcaoNotificacao {
    data class Mostrar(val notificacao: NotificacaoPush) : AcaoNotificacao()
    data class Cancelar(val episodeId: String) : AcaoNotificacao()

    /** Pedido sem prazo valido (<= 0): nao ha o que mostrar; o app responde push.ack REJEITADO. */
    object Ignorar : AcaoNotificacao()
}

/** Mapeamento puro (testado na JVM): mensagem de push ja validada -> acao sobre a notificacao. */
fun notificacaoDoPush(msg: MensagemPush): AcaoNotificacao = when (msg) {
    is MensagemPush.Solicitada ->
        if (msg.timeoutMs <= 0) {
            AcaoNotificacao.Ignorar
        } else {
            AcaoNotificacao.Mostrar(
                NotificacaoPush(
                    episodeId = msg.episodeId,
                    titulo = "Você está bem?",
                    texto = "Toque em \"Estou bem\" para avisar que está tudo certo.",
                    prazoMs = msg.timeoutMs,
                    rotuloAcao = "Estou bem"
                )
            )
        }
    is MensagemPush.Encerrada -> AcaoNotificacao.Cancelar(msg.episodeId)
}
