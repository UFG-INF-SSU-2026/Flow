package com.example.sensorapp

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.media.ToneGenerator
import android.os.CountDownTimer
import android.os.SystemClock
import android.util.Log
import android.view.View
import android.widget.Button
import android.widget.TextView

/**
 * Aviso do pedido de confirmacao de queda DENTRO da tela (Marco 3): painel, botao "Estou bem", contagem regressiva e
 * alarme (stream de ALARME).
 *
 * Quem decide e o [MonitorService] (dono do episodio: push.ack, resposta e prazo); aqui so ha a apresentacao. [exibir]
 * recebe o prazo absoluto (base de SystemClock.elapsedRealtime) e [esconder] silencia tudo. O botao nao envia nada:
 * pede ao servico que responda ([aoTocarEstouBem]).
 *
 * Todos os metodos devem ser chamados na thread PRINCIPAL.
 */
class AlertaController(
    private val context: Context,
    private val painel: View,
    private val tvAlerta: TextView,
    private val btnEstouBem: Button,
    private val aoTocarEstouBem: () -> Unit
) {
    private val audio = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private var timer: CountDownTimer? = null
    private var player: MediaPlayer? = null
    private var tom: ToneGenerator? = null
    private var volumeOriginal = -1
    private var prazoExibido = -1L

    init {
        btnEstouBem.setOnClickListener { aoTocarEstouBem() }
    }

    /** O app foi para segundo plano ou o episodio acabou: silencia e esconde (a notificacao assume, se houver). */
    fun esconder() {
        timer?.cancel()
        timer = null
        silenciar()
        painel.visibility = View.GONE
        prazoExibido = -1L
    }

    fun exibir(prazoElapsedMs: Long) {
        try {
            if (prazoExibido == prazoElapsedMs) return               // mesmo episodio: ja esta tocando
            esconder()
            val restanteMs = prazoElapsedMs - SystemClock.elapsedRealtime()
            if (restanteMs <= 0) return

            prazoExibido = prazoElapsedMs
            painel.visibility = View.VISIBLE
            tvAlerta.text = "Você está bem?"
            tocarAlarme(restanteMs)
            timer = object : CountDownTimer(restanteMs, 250) {
                override fun onTick(restanteMs: Long) {
                    tvAlerta.text = "Você está bem?\nToque no botão (${(restanteMs + 999) / 1000}s)"
                }

                override fun onFinish() {
                    esconder()      // o servico envia o SEM_RESPOSTA no prazo
                }
            }.start()
        } catch (e: Exception) {
            Log.w("AlertaController", "alerta nao exibido: ${e.javaClass.simpleName}")
        }
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
