package com.example.sensorapp

import android.os.Handler
import android.os.Looper
import android.util.Log
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONObject
import java.io.IOException

/**
 * Evento de retorno ao servidor (POST /eventos): push.ack ou confirmacao.resposta.
 *
 * E o MESMO caminho para o botao da tela e para a acao "Estou bem" da notificacao: mesmo cliente HTTP, mesmo token,
 * mesmo device_id do login, mesmo envelope AES. A identidade (user_id, device_id) sai do login, e o servidor a confere
 * com o token (divergencia e 403). Sem fila de retransmissao: se falhar, o servidor expira o episodio sozinho.
 *
 * Os callbacks rodam na thread PRINCIPAL.
 */
class EventosRetorno(
    private val auth: AuthClient,
    private val baseUrl: () -> String,
    private val deviceId: String
) {
    private val main = Handler(Looper.getMainLooper())
    private val jsonType = "application/json; charset=utf-8".toMediaType()

    /** Chamado (thread principal) quando o envio falha, para a tela mostrar o estado. Opcional. */
    @Volatile
    var aoFalhar: ((String) -> Unit)? = null

    /** [aoTerminar] recebe true so se o servidor aceitou (2xx). Falha do servidor NAO conta como resposta dada. */
    fun enviar(eventType: String, state: String, messageId: String, aoTerminar: ((Boolean) -> Unit)? = null) {
        val uid = auth.userId
        if (uid == null) {
            main.post { aoTerminar?.invoke(false) }
            return
        }
        val json = JSONObject().apply {
            put("schemaVersion", 3)
            put("user_id", uid)
            put("source", "app")
            put("device_id", deviceId)
            put("eventType", eventType)
            put("messageId", messageId)
            put("event_time", formatarIso8601(System.currentTimeMillis()))
            put("state", state)
        }
        val (nonceB64, cipherB64) = CryptoUtils.encrypt(json.toString())
        val envelope = JSONObject().put("nonce", nonceB64).put("ciphertext", cipherB64)
        val request = Request.Builder()
            .url(baseUrl() + "/eventos")
            .post(envelope.toString().toRequestBody(jsonType))
            .build()

        FlowHttp.cliente.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                main.post {
                    aoFalhar?.invoke("Falha ao enviar $eventType ao servidor")
                    aoTerminar?.invoke(false)
                }
            }

            override fun onResponse(call: Call, response: Response) {
                val ok = response.isSuccessful
                val codigo = response.code
                response.close()
                if (!ok) {
                    // 401/403/400: o servidor recusou o retorno (autenticacao ou escopo). Deixa rastro.
                    Log.w("EventosRetorno", "POST /eventos ($eventType $state) recusado: $codigo")
                }
                main.post {
                    if (!ok) aoFalhar?.invoke("Falha ao enviar $eventType ao servidor (código $codigo)")
                    aoTerminar?.invoke(ok)
                }
            }
        })
    }
}
