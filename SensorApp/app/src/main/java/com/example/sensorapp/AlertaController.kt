package com.example.sensorapp

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.media.ToneGenerator
import android.os.CountDownTimer
import android.view.View
import android.widget.Button
import android.widget.TextView
import org.json.JSONObject

/**
 * Pedido de confirmacao de queda no app (Marco 3).
 *
 * Ao receber "queda.confirmacao_solicitada": toca um alarme (stream de ALARME), mostra o botao "Estou bem" com
 * contagem regressiva de payload.timeoutMs (valor RELATIVO, vindo do servidor: nao ha constante de timeout aqui)
 * e avisa o servidor (push.ack ATIVADO). Botao -> confirmacao.resposta ESTOU_BEM; fim do tempo ->
 * confirmacao.resposta SEM_RESPOSTA. "queda.confirmacao_encerrada" (outro dispositivo ja respondeu, ou o prazo do
 * servidor acabou) para tudo SEM enviar evento.
 *
 * Todos os metodos devem ser chamados na thread PRINCIPAL.
 */
class AlertaController(
    private val context: Context,
    private val painel: View,
    private val tvAlerta: TextView,
    private val btnEstouBem: Button,
    /** (eventType, state, messageId) -> envia POST /eventos */
    private val aoResponder: (String, String, String) -> Unit
) {
    private val audio = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private var timer: CountDownTimer? = null
    private var player: MediaPlayer? = null
    private var tom: ToneGenerator? = null
    private var volumeOriginal = -1
    private var episodioAtivo: String? = null
    private var messageIdAtivo: String? = null

    init {
        btnEstouBem.setOnClickListener { responder("ESTOU_BEM") }
    }

    fun aoReceber(msg: JSONObject) {
        when (msg.getString("messageType")) {
            "queda.confirmacao_solicitada" -> iniciar(msg)
            "queda.confirmacao_encerrada" -> if (episodioAtivo != null) encerrarLocal()
        }
    }

    /** O app parou / foi para segundo plano: silencia e esconde (o servidor expira o episodio sozinho). */
    fun cancelar() = encerrarLocal()

    private fun iniciar(msg: JSONObject) {
        val episodeId = msg.getString("episodeId")
        if (episodioAtivo == episodeId) return                       // mesmo episodio: ja esta tocando
        encerrarLocal()                                              // outro episodio: recomeca

        val mid = msg.getString("messageId")
        val timeoutMs = msg.getJSONObject("payload").getLong("timeoutMs")
        if (timeoutMs <= 0) {
            aoResponder("push.ack", "REJEITADO", mid)
            return
        }

        episodioAtivo = episodeId
        messageIdAtivo = mid
        painel.visibility = View.VISIBLE
        tvAlerta.text = "Você está bem?"
        tocarAlarme(timeoutMs)
        aoResponder("push.ack", "ATIVADO", mid)

        timer = object : CountDownTimer(timeoutMs, 250) {
            override fun onTick(restanteMs: Long) {
                tvAlerta.text = "Você está bem?\nToque no botão (${(restanteMs + 999) / 1000}s)"
            }

            override fun onFinish() {
                responder("SEM_RESPOSTA")
            }
        }.start()
    }

    private fun responder(estado: String) {
        val mid = messageIdAtivo ?: return
        encerrarLocal()
        aoResponder("confirmacao.resposta", estado, mid)
    }

    private fun encerrarLocal() {
        timer?.cancel()
        timer = null
        silenciar()
        painel.visibility = View.GONE
        episodioAtivo = null
        messageIdAtivo = null
    }

    // ---------------------------------------------------------------- som
    private fun tocarAlarme(timeoutMs: Long) {
        // Garante que o alarme seja audivel: sobe o volume do stream de ALARME (restaurado em silenciar()).
        try {
            volumeOriginal = audio.getStreamVolume(AudioManager.STREAM_ALARM)
            audio.setStreamVolume(AudioManager.STREAM_ALARM, audio.getStreamMaxVolume(AudioManager.STREAM_ALARM), 0)
        } catch (e: Exception) {
            volumeOriginal = -1
        }
        try {
            val uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
                ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
            player = MediaPlayer().apply {
                setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ALARM)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build()
                )
                setDataSource(context, uri)
                isLooping = true
                prepare()
                start()
            }
        } catch (e: Exception) {
            // Sem toque padrao: usa um tom de alerta do proprio sistema
            player?.release()
            player = null
            try {
                tom = ToneGenerator(AudioManager.STREAM_ALARM, 100).also {
                    it.startTone(ToneGenerator.TONE_CDMA_ALERT_CALL_GUARD, timeoutMs.toInt())
                }
            } catch (e2: Exception) {
                tom = null
            }
        }
    }

    private fun silenciar() {
        try { player?.stop() } catch (e: Exception) { /* ja parado */ }
        player?.release()
        player = null
        tom?.release()
        tom = null
        if (volumeOriginal >= 0) {
            try { audio.setStreamVolume(AudioManager.STREAM_ALARM, volumeOriginal, 0) } catch (e: Exception) { }
            volumeOriginal = -1
        }
    }
}
