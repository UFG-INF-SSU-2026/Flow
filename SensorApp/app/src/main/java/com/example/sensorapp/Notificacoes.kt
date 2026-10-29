package com.example.sensorapp

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.RingtoneManager
import android.net.Uri
import android.os.Build
import androidx.core.app.NotificationCompat

/** Canais e construcao das notificacoes do app: o aviso persistente do servico e o pedido de confirmacao. */
object Notificacoes {
    const val CANAL_ALERTA = "alerta_queda"
    const val CANAL_MONITORAMENTO = "monitoramento"
    const val ID_SERVICO = 1
    const val ID_ALERTA = 2

    private const val FLAGS_PI = PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT

    private fun somAlarme(): Uri? =
        RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
            ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)

    /** Cria os dois canais (Android 8 ou superior). Idempotente: chamado na subida do app. */
    fun criarCanais(ctx: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val nm = ctx.getSystemService(NotificationManager::class.java) ?: return

        val alerta = NotificationChannel(CANAL_ALERTA, "Pedido de confirmação de queda", NotificationManager.IMPORTANCE_HIGH).apply {
            description = "Avisa com som e vibração quando o sistema pergunta se você está bem."
            enableVibration(true)
            vibrationPattern = longArrayOf(0, 500, 300, 500)
            lockscreenVisibility = Notification.VISIBILITY_PUBLIC
            setSound(
                somAlarme(),
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                    .build()
            )
        }
        val monitoramento = NotificationChannel(CANAL_MONITORAMENTO, "Monitoramento", NotificationManager.IMPORTANCE_LOW).apply {
            description = "Aviso discreto de que o monitoramento está ligado."
            setShowBadge(false)
        }
        nm.createNotificationChannels(listOf(alerta, monitoramento))
    }

    private fun abrirApp(ctx: Context): PendingIntent {
        val intent = Intent(ctx, MainActivity::class.java).addFlags(
            Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        )
        return PendingIntent.getActivity(ctx, 12, intent, FLAGS_PI)
    }

    private fun paraOServico(ctx: Context, requestCode: Int, acao: String): PendingIntent =
        PendingIntent.getService(ctx, requestCode, Intent(ctx, MonitorService::class.java).setAction(acao), FLAGS_PI)

    /** Notificacao persistente do servico em primeiro plano; [estadoPush] vem do PushClient. */
    fun servico(ctx: Context, estadoPush: String): Notification {
        val texto = when (estadoPush) {
            "conectado" -> "Conectado ao servidor"
            "conectando", "reconectando" -> "Reconectando…"
            "desconectado" -> "Desconectado"
            else -> estadoPush.replaceFirstChar { it.uppercase() }
        }
        return NotificationCompat.Builder(ctx, CANAL_MONITORAMENTO)
            .setSmallIcon(android.R.drawable.ic_menu_compass)
            .setContentTitle("Monitoramento ativo")
            .setContentText(texto)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .setContentIntent(abrirApp(ctx))
            .addAction(0, "Parar", paraOServico(ctx, 10, MonitorService.ACAO_PARAR))
            .build()
    }

    /** Pedido de confirmacao: som e vibracao, visivel na tela de bloqueio, com a acao "Estou bem". */
    fun alerta(ctx: Context, n: NotificacaoPush): Notification {
        val b = NotificationCompat.Builder(ctx, CANAL_ALERTA)
            .setSmallIcon(android.R.drawable.ic_dialog_alert)
            .setContentTitle(n.titulo)
            .setContentText(n.texto)
            .setCategory(NotificationCompat.CATEGORY_ALARM)
            .setPriority(NotificationCompat.PRIORITY_MAX)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setOngoing(true)
            .setTimeoutAfter(n.prazoMs)
            .setContentIntent(abrirApp(ctx))
            .addAction(0, n.rotuloAcao, paraOServico(ctx, 11, MonitorService.ACAO_ESTOU_BEM))
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            // Antes do Android 8 nao ha canal: som e vibracao vao na propria notificacao.
            b.setSound(somAlarme(), AudioManager.STREAM_ALARM).setVibrate(longArrayOf(0, 500, 300, 500))
        }
        val notificacao = b.build()
        notificacao.flags = notificacao.flags or Notification.FLAG_INSISTENT   // repete o som ate responder ou vencer o prazo
        return notificacao
    }
}
