package com.example.sensorapp

import android.app.Application
import android.content.SharedPreferences
import android.provider.Settings

/**
 * Estado do PROCESSO, compartilhado entre a tela ([MainActivity]) e o servico ([MonitorService]): sessao, cliente
 * HTTP unico, eventos de retorno e localizacao. Assim a tela e o servico usam o mesmo token e o mesmo device_id.
 */
class FlowApp : Application() {

    lateinit var prefs: SharedPreferences
        private set

    val deviceId: String by lazy {
        Settings.Secure.getString(contentResolver, Settings.Secure.ANDROID_ID) ?: "desconhecido"
    }

    /** Quem estiver na tela registra aqui o que fazer quando o refresh e recusado (chamado em qualquer thread). */
    @Volatile
    var aoSessaoExpirada: (() -> Unit)? = null

    val auth: AuthClient by lazy {
        AuthClient(prefs, { baseUrl() }, deviceId).also { a ->
            a.onSessaoExpirada = {
                MonitorService.parar(this@FlowApp)
                aoSessaoExpirada?.invoke()
            }
        }
    }

    val eventos: EventosRetorno by lazy { EventosRetorno(auth, { baseUrl() }, deviceId) }

    val localizacao: Localizacao by lazy { Localizacao(this) }

    /** URL base do servidor (IP e porta vem das preferencias). So HTTPS (res/xml/network_security_config.xml). */
    fun baseUrl(): String {
        val ip = prefs.getString("server_ip", null) ?: "localhost"
        val porta = prefs.getString("server_port", null) ?: "5000"
        return "https://$ip:$porta"
    }

    override fun onCreate() {
        super.onCreate()
        prefs = getSharedPreferences("sensor_app_prefs", MODE_PRIVATE)
        FlowHttp.iniciar(auth)
        Notificacoes.criarCanais(this)
    }
}
