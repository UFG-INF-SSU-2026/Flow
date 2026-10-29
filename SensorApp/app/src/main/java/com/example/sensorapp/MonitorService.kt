package com.example.sensorapp

import android.annotation.SuppressLint
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat

/**
 * Servico em primeiro plano do monitoramento. Hospeda o canal de push (SSE, [PushClient]) enquanto o monitoramento
 * estiver ligado, para o pedido de confirmacao de queda chegar com o app em segundo plano ou a tela apagada.
 * Sem FCM: continua sendo o app quem abre a conexao com o servidor.
 *
 * E o dono do episodio de confirmacao em curso: envia push.ack, agenda o SEM_RESPOSTA do prazo e responde
 * (confirmacao.resposta) tanto pelo botao da tela quanto pela acao "Estou bem" da notificacao. Com a tela visivel, o
 * aviso e o alarme ficam na tela; sem ela, vira notificacao (com som e vibracao pelo canal).
 *
 * Todos os callbacks e metodos rodam na thread PRINCIPAL.
 */
class MonitorService : Service() {

    /** A tela implementa isto para acompanhar o servico. Chamado na thread principal. */
    interface Ouvinte {
        fun aoEstadoPush(texto: String)

        /** [episodio] null = encerrado. */
        fun aoEpisodio(episodio: Episodio?)

        /** O servico parou por conta propria (acao "Parar" da notificacao). */
        fun aoParar()
    }

    /** [prazoElapsedMs] e absoluto, na base de [SystemClock.elapsedRealtime]. */
    data class Episodio(val messageId: String, val episodeId: String, val prazoElapsedMs: Long)

    private val main = Handler(Looper.getMainLooper())
    private var push: PushClient? = null
    private var gps = false
    private var planoAtual: NotificacaoPush? = null
    private var enviandoResposta = false
    private val aoExpirar = Runnable { responderEpisodio("SEM_RESPOSTA") }

    private val app: FlowApp get() = application as FlowApp

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        instancia = this
        Notificacoes.criarCanais(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACAO_ESTOU_BEM -> {
                if (rodando) responderEpisodio("ESTOU_BEM") else stopSelf()
            }
            ACAO_PARAR -> {
                ouvinte?.aoParar()
                stopSelf()
            }
            ACAO_ATUALIZAR -> {
                gps = intent?.getBooleanExtra(EXTRA_GPS, false) ?: false
                if (rodando) subirPrimeiroPlano() else stopSelf()
            }
            ACAO_INICIAR -> {
                gps = intent?.getBooleanExtra(EXTRA_GPS, false) ?: false
                if (subirPrimeiroPlano() && !rodando) {
                    rodando = true
                    estadoPush = "conectando"
                    push = PushClient(
                        app.auth,
                        { "${app.baseUrl()}/push/app" },
                        { msg -> aoMensagem(msg) },
                        { texto -> aoEstado(texto) }
                    ).also { it.conectar() }
                }
            }
            else -> stopSelf()    // reinicio sem comando (o servico nao e sticky)
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        push?.desconectar()
        push = null
        main.removeCallbacksAndMessages(null)
        episodio = null
        rodando = false
        estadoPush = "desconectado"
        instancia = null
        NotificationManagerCompat.from(this).cancel(Notificacoes.ID_ALERTA)
        super.onDestroy()
    }

    // ---------------------------------------------------------------- primeiro plano
    /** Declara o tipo de servico (Android 14 exige): specialUse, mais location se o GPS estiver ligado e permitido. */
    private fun tipoPrimeiroPlano(): Int {
        val comGps = gps && Localizacao.temPermissao(this)
        if (Build.VERSION.SDK_INT >= 34) {
            var tipo = ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
            if (comGps) tipo = tipo or ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION
            return tipo
        }
        if (Build.VERSION.SDK_INT >= 29 && comGps) return ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION
        return 0
    }

    private fun subirPrimeiroPlano(): Boolean {
        return try {
            ServiceCompat.startForeground(this, Notificacoes.ID_SERVICO, Notificacoes.servico(this, estadoPush), tipoPrimeiroPlano())
            true
        } catch (e: Exception) {
            Log.w(TAG, "nao foi possivel manter o servico em primeiro plano: ${e.javaClass.simpleName}")
            ouvinte?.aoParar()
            stopSelf()
            false
        }
    }

    // ---------------------------------------------------------------- push
    private fun aoEstado(texto: String) {
        if (!rodando || texto == estadoPush) return
        estadoPush = texto
        notificar(Notificacoes.ID_SERVICO) { Notificacoes.servico(this, texto) }
        ouvinte?.aoEstadoPush(texto)
    }

    private fun aoMensagem(msg: MensagemPush) {
        when (val acao = notificacaoDoPush(msg)) {
            is AcaoNotificacao.Mostrar -> if (msg is MensagemPush.Solicitada) iniciarEpisodio(msg, acao.notificacao)
            is AcaoNotificacao.Cancelar -> if (episodio?.episodeId == acao.episodeId) encerrarEpisodio()
            AcaoNotificacao.Ignorar ->
                if (msg is MensagemPush.Solicitada) app.eventos.enviar("push.ack", "REJEITADO", msg.messageId)
        }
    }

    // ---------------------------------------------------------------- episodio de confirmacao
    private fun iniciarEpisodio(msg: MensagemPush.Solicitada, plano: NotificacaoPush) {
        if (episodio?.episodeId == msg.episodeId) return          // mesmo episodio: ja esta em curso
        if (episodio != null) encerrarEpisodio()                  // outro episodio: recomeca

        val ep = Episodio(msg.messageId, msg.episodeId, SystemClock.elapsedRealtime() + msg.timeoutMs)
        episodio = ep
        planoAtual = plano
        enviandoResposta = false
        app.eventos.enviar("push.ack", "ATIVADO", msg.messageId)
        main.postDelayed(aoExpirar, msg.timeoutMs)
        if (!telaVisivel) mostrarNotificacao(plano)
        ouvinte?.aoEpisodio(ep)
    }

    /**
     * [estado] "ESTOU_BEM" (botao da tela ou acao da notificacao) ou "SEM_RESPOSTA" (prazo). O "Estou bem" so encerra
     * o episodio quando o servidor ACEITA: falha do servidor nao e resposta dada, e o aviso continua ate o prazo.
     */
    fun responderEpisodio(estado: String) {
        val ep = episodio ?: return
        if (estado == "SEM_RESPOSTA") {
            encerrarEpisodio()
            app.eventos.enviar("confirmacao.resposta", estado, ep.messageId)
            return
        }
        if (enviandoResposta) return
        enviandoResposta = true
        app.eventos.enviar("confirmacao.resposta", estado, ep.messageId) { ok ->
            enviandoResposta = false
            if (ok && episodio?.messageId == ep.messageId) encerrarEpisodio()
        }
    }

    private fun encerrarEpisodio() {
        main.removeCallbacks(aoExpirar)
        episodio = null
        planoAtual = null
        enviandoResposta = false
        NotificationManagerCompat.from(this).cancel(Notificacoes.ID_ALERTA)
        ouvinte?.aoEpisodio(null)
    }

    private fun aoMudarVisibilidade() {
        val ep = episodio ?: return
        if (telaVisivel) {
            NotificationManagerCompat.from(this).cancel(Notificacoes.ID_ALERTA)
        } else {
            val restanteMs = ep.prazoElapsedMs - SystemClock.elapsedRealtime()
            if (restanteMs > 0) planoAtual?.let { mostrarNotificacao(it.copy(prazoMs = restanteMs)) }
        }
    }

    // ---------------------------------------------------------------- notificacoes
    private fun mostrarNotificacao(plano: NotificacaoPush) {
        notificar(Notificacoes.ID_ALERTA) { Notificacoes.alerta(this, plano) }
    }

    /** Sem a permissao de notificacao nada e mostrado e o app segue funcionando (alarme dentro da tela). */
    @SuppressLint("MissingPermission")
    private fun notificar(id: Int, construir: () -> android.app.Notification) {
        val nm = NotificationManagerCompat.from(this)
        if (!nm.areNotificationsEnabled()) return
        try {
            nm.notify(id, construir())
        } catch (e: SecurityException) {
            Log.w(TAG, "notificacao nao mostrada (sem permissao)")
        }
    }

    companion object {
        private const val TAG = "MonitorService"
        const val ACAO_INICIAR = "com.example.sensorapp.monitor.INICIAR"
        const val ACAO_PARAR = "com.example.sensorapp.monitor.PARAR"
        const val ACAO_ESTOU_BEM = "com.example.sensorapp.monitor.ESTOU_BEM"
        const val ACAO_ATUALIZAR = "com.example.sensorapp.monitor.ATUALIZAR"
        private const val EXTRA_GPS = "gps"

        @Volatile
        var rodando = false
            private set

        @Volatile
        var estadoPush = "desconectado"
            private set

        /** Episodio de confirmacao em curso (a tela le isto ao voltar para o primeiro plano). */
        @Volatile
        var episodio: Episodio? = null
            private set

        /** Tela interessada nos eventos; enquanto existir uma [MainActivity], ela se registra aqui. */
        @Volatile
        var ouvinte: Ouvinte? = null

        /** true entre onStart e onStop da tela: com ela visivel, o aviso e o alarme ficam na tela, sem notificacao. */
        @Volatile
        var telaVisivel = false
            private set

        @Volatile
        private var instancia: MonitorService? = null

        /** [comGps]: a coleta de localizacao esta ligada (declara o tipo de servico "location"). */
        fun iniciar(ctx: Context, comGps: Boolean) {
            val intent = Intent(ctx, MonitorService::class.java).setAction(ACAO_INICIAR).putExtra(EXTRA_GPS, comGps)
            ContextCompat.startForegroundService(ctx, intent)
        }

        fun parar(ctx: Context) {
            ctx.stopService(Intent(ctx, MonitorService::class.java))
        }

        /** O GPS foi ligado ou desligado com o servico rodando: reaplica o tipo de servico. */
        fun atualizarTipo(ctx: Context, comGps: Boolean) {
            if (!rodando) return
            ctx.startService(Intent(ctx, MonitorService::class.java).setAction(ACAO_ATUALIZAR).putExtra(EXTRA_GPS, comGps))
        }

        fun responder(estado: String) {
            instancia?.responderEpisodio(estado)
        }

        fun definirTelaVisivel(visivel: Boolean) {
            telaVisivel = visivel
            instancia?.aoMudarVisibilidade()
        }
    }
}
