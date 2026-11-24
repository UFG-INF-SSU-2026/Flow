package com.example.sensorapp

import android.content.Context
import android.graphics.drawable.ColorDrawable
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.media.AudioAttributes
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.speech.tts.TextToSpeech
import android.view.View
import android.widget.TextView
import java.util.Locale

/*
 * Atuadores concretos do pedido de confirmacao de queda (equivalem ao buzzer e aos LEDs do firmware). Todos seguem o
 * contrato de [Atuador]: idempotentes, e podem lancar excecao (o [Atuadores] isola). Rodam na thread PRINCIPAL.
 * Dependem de aparelho (camera, vibrador, voz): sem teste automatizado, validados em aparelho fisico.
 */

/** Estado visual vermelho: o painel do aviso fica vermelho forte e o texto claro. Nao mexe em visibilidade nem em texto. */
class AtuadorVisual(private val painel: View, private val tvAlerta: TextView) : Atuador {
    // Valores originais do layout, guardados na construcao (o controller e criado uma vez, com o layout intacto)
    private val fundoOriginal = painel.background
    private val corTextoOriginal = tvAlerta.currentTextColor

    override fun ligar() {
        // Novo drawable (nao setBackgroundColor): este recolore o ColorDrawable original no lugar e o fundo nao voltaria
        painel.background = ColorDrawable(VERMELHO)
        tvAlerta.setTextColor(TEXTO_CLARO)
    }

    override fun desligar() {
        painel.background = fundoOriginal
        tvAlerta.setTextColor(corTextoOriginal)
    }

    private companion object {
        const val VERMELHO = 0xFFC62828.toInt()
        const val TEXTO_CLARO = 0xFFFFFFFF.toInt()
    }
}

/** Vibracao em laco, com o mesmo padrao do canal de notificacao, ate [desligar]. Aparelho sem vibrador: nao faz nada. */
class AtuadorVibracao(context: Context) : Atuador {
    private val vibrador: Vibrator? =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            (context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as? VibratorManager)?.defaultVibrator
        } else {
            @Suppress("DEPRECATION")
            context.getSystemService(Context.VIBRATOR_SERVICE) as? Vibrator
        }
    private var vibrando = false

    @Suppress("DEPRECATION")
    override fun ligar() {
        val v = vibrador ?: return
        if (!v.hasVibrator()) return
        val atributos = AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_ALARM)
            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
            .build()
        vibrando = true
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            v.vibrate(VibrationEffect.createWaveform(PADRAO_MS, 0), atributos)       // repete do indice 0
        } else {
            v.vibrate(PADRAO_MS, 0, atributos)
        }
    }

    override fun desligar() {
        if (!vibrando) return        // so cancela o que este atuador iniciou
        vibrando = false
        vibrador?.cancel()
    }

    private companion object {
        // Mesmo padrao do canal "alerta_queda" (pausa, vibra, pausa, vibra)
        val PADRAO_MS = longArrayOf(0, 500, 300, 500)
    }
}

/**
 * Lanterna fixa (nao pisca) da camera traseira. A camera com flash e procurada na primeira chamada de [ligar] e o
 * resultado e guardado, inclusive o "nao ha". O modo lanterna nao pede a permissao de camera (API 23 ou superior).
 */
class AtuadorLanterna(context: Context) : Atuador {
    private val cameras = context.getSystemService(Context.CAMERA_SERVICE) as? CameraManager
    private var procurou = false
    private var idDoFlash: String? = null
    private var acesa = false

    override fun ligar() {
        val gerente = cameras ?: return
        val id = localizarFlash(gerente) ?: return
        gerente.setTorchMode(id, true)
        acesa = true                 // so depois de o sistema aceitar
    }

    override fun desligar() {
        if (!acesa) return
        try {
            val id = idDoFlash
            if (id != null) cameras?.setTorchMode(id, false)
        } finally {
            acesa = false            // mesmo que apagar falhe, o estado nao fica preso em "acesa"
        }
    }

    private fun localizarFlash(gerente: CameraManager): String? {
        if (!procurou) {
            val comFlash = gerente.cameraIdList.filter {
                gerente.getCameraCharacteristics(it).get(CameraCharacteristics.FLASH_INFO_AVAILABLE) == true
            }
            idDoFlash = comFlash.firstOrNull {
                gerente.getCameraCharacteristics(it).get(CameraCharacteristics.LENS_FACING) == CameraCharacteristics.LENS_FACING_BACK
            } ?: comFlash.firstOrNull()
            procurou = true          // se a busca lancar, tenta de novo na proxima vez
        }
        return idDoFlash
    }
}

/**
 * Fala [frase] uma unica vez ao ligar, em pt-BR. A inicializacao do motor de voz e assincrona: se [desligar] chegar
 * antes (ou o motor for outro), o retorno atrasado nao fala. Sem motor, sem idioma ou sem dados de voz: desiste em
 * silencio e libera o motor.
 */
class AtuadorFala(private val context: Context, private val frase: String) : Atuador {
    private val trava = Any()
    private var ativo = false
    private var geracao = 0          // identifica cada ligar(): o retorno de um motor antigo nao age sobre o novo
    private var motor: TextToSpeech? = null

    override fun ligar() {
        synchronized(trava) {
            if (ativo) return
            ativo = true
            val minha = ++geracao
            motor = TextToSpeech(context.applicationContext) { status -> aoIniciar(minha, status) }
        }
    }

    override fun desligar() {
        val m = synchronized(trava) {
            ativo = false
            motor.also { motor = null }
        } ?: return
        try {
            m.stop()
        } finally {
            m.shutdown()
        }
    }

    // Pode rodar em outra thread: sem View aqui, e nenhuma excecao pode escapar (nao ha quem isole)
    private fun aoIniciar(minha: Int, status: Int) {
        try {
            val m = synchronized(trava) { if (ativo && minha == geracao) motor else null } ?: return
            val idioma = if (status == TextToSpeech.SUCCESS) m.setLanguage(Locale.forLanguageTag("pt-BR")) else null
            if (idioma == null || idioma == TextToSpeech.LANG_MISSING_DATA || idioma == TextToSpeech.LANG_NOT_SUPPORTED) {
                liberar(m)
                return
            }
            // Stream de ALARME: o AlertaController sobe o volume dele; no de midia a fala poderia ficar baixa
            m.setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build()
            )
            m.speak(frase, TextToSpeech.QUEUE_FLUSH, null, "pedido_confirmacao")
        } catch (e: Exception) {
            // motor encerrado no meio (desligar concorrente) ou indisponivel: sem fala
        }
    }

    private fun liberar(m: TextToSpeech) {
        synchronized(trava) { if (motor === m) motor = null }
        m.shutdown()
    }
}
