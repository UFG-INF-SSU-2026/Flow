package com.example.sensorapp

import android.os.Handler
import android.os.Looper
import okhttp3.Request
import okhttp3.Response
import okhttp3.sse.EventSource
import okhttp3.sse.EventSourceListener
import okhttp3.sse.EventSources
import org.json.JSONObject

/**
 * Canal de push servidor -> app (Marco 3), por SSE.
 *
 * O APP abre a conexao (GET /push/app) e o servidor empurra mensagens por ela: o celular nao expoe nenhuma porta.
 * O canal existe enquanto o monitoramento esta ativo: quem o hospeda e o [MonitorService] (servico em primeiro plano),
 * que chama conectar() ao iniciar e desconectar() ao parar.
 *
 * Callbacks [onMensagem] e [onEstado] sao chamados na thread PRINCIPAL. [onMensagem] so recebe mensagem de forma
 * valida ([validarPush]); o que estiver fora do formato e descartado aqui, em silencio.
 */
class PushClient(
    private val auth: AuthClient,
    private val urlPush: () -> String,
    private val onMensagem: (MensagemPush) -> Unit,
    private val onEstado: (String) -> Unit
) {
    private val main = Handler(Looper.getMainLooper())

    // Variante SSE do cliente unico (readTimeout > keepalive do servidor, sem prazo total de chamada).
    private val client = FlowHttp.sse()

    private var eventSource: EventSource? = null

    @Volatile private var ativo = false
    @Volatile private var lastEventId = 0L        // os ids do SSE passam de Int.MAX_VALUE em 2038 (docs/schemas.md, secao 6)
    @Volatile private var backoffMs = 1_000L
    private val vistos = ArrayDeque<String>()     // messageIds ja tratados (idempotencia na reconexao)

    // Renovacao disparada por "reautenticar": a reconexao so abre DEPOIS que ela termina, para sair ja com o token novo.
    // Sem isso, uma renovacao mais lenta que o atraso da reconexao (1 s) faria o app reconectar com o token antigo, o
    // servidor responderia "reautenticar" de novo e fecharia, em laco, ate a renovacao acabar.
    private val lockRenovacao = Any()
    private var renovando = false                 // sob lockRenovacao
    private var reconexaoPendente = false         // sob lockRenovacao: abrir() foi chamado enquanto renovava

    fun conectar() {
        if (ativo) return
        ativo = true
        backoffMs = 1_000L
        abrir()
    }

    fun desconectar() {
        ativo = false
        main.removeCallbacksAndMessages(null)
        eventSource?.cancel()
        eventSource = null
        estado("desconectado")
    }

    private fun abrir() {
        if (!ativo) return
        synchronized(lockRenovacao) {
            if (renovando) {                      // espera a renovacao: ao terminar, ela chama abrir() de novo
                reconexaoPendente = true
                return
            }
        }
        estado("conectando")
        val b = Request.Builder().url(urlPush()).header("Accept", "text/event-stream")
        if (lastEventId > 0) b.header("Last-Event-ID", lastEventId.toString())
        eventSource = EventSources.createFactory(client).newEventSource(b.build(), listener)
    }

    private fun reconectar(atrasoMs: Long) {
        if (!ativo) return
        main.removeCallbacksAndMessages(null)
        eventSource?.cancel()
        estado("reconectando")
        main.postDelayed({ abrir() }, atrasoMs)
    }

    private fun estado(texto: String) {
        main.post { onEstado(texto) }
    }

    /** Renova o token fora da thread do OkHttp; se abrir() foi chamado nesse meio tempo, reabre quando terminar. */
    private fun renovarAntesDeReconectar() {
        synchronized(lockRenovacao) {
            if (renovando) return                 // ja ha uma renovacao em curso
            renovando = true
        }
        Thread {
            try {
                auth.renovar(null)
            } finally {
                val reabrir = synchronized(lockRenovacao) {
                    renovando = false
                    val pendente = reconexaoPendente
                    reconexaoPendente = false
                    pendente
                }
                if (reabrir) main.post { abrir() }    // abrir() confere "ativo" (o app pode ter parado nesse meio tempo)
            }
        }.start()
    }

    private val listener = object : EventSourceListener() {
        override fun onOpen(eventSource: EventSource, response: Response) {
            backoffMs = 1_000L
            estado("conectado")
        }

        override fun onEvent(eventSource: EventSource, id: String?, type: String?, data: String) {
            when (type) {
                "push" -> tratarPush(id, data)
                // Token perto de vencer: renova; o servidor fecha o stream em seguida (onClosed reconecta, mas so
                // abre depois que a renovacao terminar).
                "reautenticar" -> renovarAntesDeReconectar()
            }
        }

        override fun onClosed(eventSource: EventSource) {
            reconectar(1_000L)
        }

        override fun onFailure(eventSource: EventSource, t: Throwable?, response: Response?) {
            when (response?.code) {
                401 -> {
                    // O authenticator ja tentou renovar. Sessao apagada = refresh RECUSADO (onSessaoExpirada ja avisou a
                    // UI e parou o envio): nao adianta insistir. Sessao ainda la = a renovacao falhou por rede (servidor
                    // fora do ar, por exemplo): falha transitoria, segue para o backoff abaixo e reconecta sozinho.
                    if (auth.sessao == null) { ativo = false; estado("sessão expirada"); return }
                }
                403 -> { ativo = false; estado("sem permissão"); return }     // nao adianta insistir
            }
            val atraso = backoffMs
            backoffMs = minOf(backoffMs * 2, 30_000L)
            reconectar(atraso)
        }
    }

    private fun tratarPush(id: String?, data: String) {
        try {
            val env = JSONObject(data)
            val msg = JSONObject(CryptoUtils.decrypt(env.getString("nonce"), env.getString("ciphertext")))
            id?.toLongOrNull()?.let { lastEventId = it }
            if (msg.optInt("schemaVersion") != 3) return
            if (msg.optInt("userId", -1) != auth.userId) return          // defesa em profundidade
            // A forma e conferida AQUI, antes do post: depois dele o codigo roda na thread principal, onde uma
            // JSONException (mensagem autentica, mas fora do formato) derrubaria o app.
            val push = validarPush(msg) ?: return
            if (vistos.contains(push.messageId)) return
            vistos.addLast(push.messageId)
            if (vistos.size > 100) vistos.removeFirst()
            main.post { onMensagem(push) }
        } catch (e: Exception) {
            // mensagem malformada ou que nao decifra: descarta
        }
    }
}
