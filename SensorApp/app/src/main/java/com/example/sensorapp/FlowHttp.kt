package com.example.sensorapp

import okhttp3.OkHttpClient
import java.util.concurrent.TimeUnit

/**
 * Cliente HTTP unico do app: telemetria, eventos de retorno, rotas de autenticacao (/auth/...) e o canal SSE saem daqui.
 *
 * O cliente base leva o interceptor de autorizacao e o autenticador de 401 (renova e repete UMA vez) do [AuthClient].
 * As variantes saem de [OkHttpClient.newBuilder] e compartilham pool de conexoes e configuracao TLS: a unica
 * confianca e a CA do projeto (res/xml/network_security_config.xml); nao ha TrustManager nem verificador de nome proprios.
 */
object FlowHttp {
    @Volatile
    private var base: OkHttpClient? = null

    /** Cria o cliente base (idempotente). Chamado uma vez, na subida do processo ([FlowApp]). */
    @Synchronized
    fun iniciar(auth: AuthClient) {
        if (base != null) return
        base = OkHttpClient.Builder()
            .connectTimeout(5, TimeUnit.SECONDS)
            .readTimeout(8, TimeUnit.SECONDS)
            .callTimeout(15, TimeUnit.SECONDS)
            .addInterceptor(auth.interceptor)
            .authenticator(auth.authenticator)
            .build()
    }

    /** Chamadas curtas (login, refresh, telemetria, eventos). */
    val cliente: OkHttpClient
        get() = base ?: error("FlowHttp.iniciar() nao foi chamado")

    /**
     * Variante do canal de push (SSE): sem prazo total de chamada (o stream fica aberto). O readTimeout fica acima do
     * keepalive do servidor (15 s): detecta conexao morta sem derrubar um stream saudavel (com 0, uma conexao
     * perdida em silencio ficaria pendurada para sempre).
     */
    fun sse(): OkHttpClient = cliente.newBuilder()
        .readTimeout(40, TimeUnit.SECONDS)
        .callTimeout(0, TimeUnit.SECONDS)
        .build()
}
