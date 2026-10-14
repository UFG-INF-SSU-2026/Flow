package com.example.sensorapp

import android.os.Handler
import android.os.Looper
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.sse.EventSource
import okhttp3.sse.EventSourceListener
import okhttp3.sse.EventSources
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * Canal de push servidor -> app (Marco 3), por SSE.
 *
 * O APP abre a conexao (GET /push/app) e o servidor empurra mensagens por ela: o celular nao expoe nenhuma porta.
 * O canal existe so enquanto o monitoramento esta ativo e o app em primeiro plano (conectar() em iniciarEnvio,
 * desconectar() em pararEnvio/onPause).
 *
 * Callbacks [onMensagem] e [onEstado] sao chamados na thread PRINCIPAL.
 */
class PushClient(
    private val auth: AuthClient,
    private val urlPush: () -> String,
    private val onMensagem: (JSONObject) -> Unit,
    private val onEstado: (String) -> Unit
) {
    private val main = Handler(Looper.getMainLooper())

    // readTimeout > keepalive do servidor (15 s): detecta conexao morta sem derrubar um stream saudavel.
    // (O padrao do OkHttp, 10 s, derrubaria o stream a cada intervalo entre keepalives.)
    private val client = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(40, TimeUnit.SECONDS)
        .addInterceptor(auth.interceptor)
        .authenticator(auth.authenticator)
        .build()

    private var eventSource: EventSource? = null

    @Volatile private var ativo = false
    @Volatile private var lastEventId = 0
    @Volatile private var backoffMs = 1_000L
    private val vistos = ArrayDeque<String>()     // messageIds ja tratados (idempotencia na reconexao)

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

    private val listener = object : EventSourceListener() {
        override fun onOpen(eventSource: EventSource, response: Response) {
            backoffMs = 1_000L
            estado("conectado")
        }

        override fun onEvent(eventSource: EventSource, id: String?, type: String?, data: String) {
            when (type) {
                "push" -> tratarPush(id, data)
                // Token perto de vencer: renova; o servidor fecha o stream em seguida (onClosed reconecta).
                "reautenticar" -> Thread { auth.renovar(null) }.start()
            }
        }

        override fun onClosed(eventSource: EventSource) {
            reconectar(1_000L)
        }

        override fun onFailure(eventSource: EventSource, t: Throwable?, response: Response?) {
            when (response?.code) {
                401 -> { ativo = false; estado("sessão expirada"); return }   // o authenticator ja tentou renovar
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
            id?.toIntOrNull()?.let { lastEventId = it }
            if (msg.optInt("schemaVersion") != 3) return
            if (msg.optInt("userId", -1) != auth.userId) return          // defesa em profundidade
            val mid = msg.getString("messageId")
            if (vistos.contains(mid)) return
            vistos.addLast(mid)
            if (vistos.size > 100) vistos.removeFirst()
            main.post { onMensagem(msg) }
        } catch (e: Exception) {
            // mensagem malformada ou que nao decifra: descarta
        }
    }
}
