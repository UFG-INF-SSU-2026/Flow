package com.example.sensorapp

import android.content.Context
import okhttp3.Interceptor
import okhttp3.OkHttpClient
import okhttp3.Response
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Cliente HTTP unico do app: telemetria, eventos de retorno, rotas de autenticacao (/auth/...) e o canal SSE saem daqui.
 *
 * O cliente base leva o interceptor de autorizacao e o autenticador de 401 (renova e repete UMA vez) do [AuthClient].
 * As variantes saem de [OkHttpClient.newBuilder] e compartilham pool de conexoes e configuracao TLS: a unica
 * confianca e a CA do projeto ([CaProjeto]: a importada pela pessoa ou a embutida). O verificador de nome do servidor
 * e o padrao do OkHttp (ligado). Quando o app tem certificado de cliente ([EmissorCertificado]), ele e apresentado em
 * toda conexao nova; ao emitir, trocar ou descartar o certificado, [recarregarTls] abre conexoes novas.
 */
object FlowHttp {
    @Volatile
    private var base: OkHttpClient? = null
    private var auth: AuthClient? = null
    private var contexto: Context? = null
    private var emissor: (() -> EmissorCertificado)? = null

    /** Cria o cliente base (idempotente). Chamado uma vez, na subida do processo ([FlowApp]). */
    @Synchronized
    fun iniciar(auth: AuthClient, contexto: Context, emissor: () -> EmissorCertificado) {
        if (base != null) return
        this.auth = auth
        this.contexto = contexto.applicationContext
        this.emissor = emissor
        base = construir()
    }

    /**
     * A CA ou o certificado de cliente mudou: refaz o cliente e descarta as conexoes abertas (feitas com a confianca ou
     * o certificado antigos; o certificado so vale na conexao em que foi apresentado). Variantes ja criadas valem ate
     * serem recriadas (o canal de push as recria a cada conexao).
     */
    @Synchronized
    fun recarregarTls() {
        if (base == null) return
        val antigo = base
        base = construir()
        antigo?.connectionPool?.evictAll()
    }

    private fun construir(): OkHttpClient {
        val gerente = emissor!!().gerenteDeChave()      // leitura sem trava: nao ha impasse com a emissao
        val (fabrica, confianca) = CaProjeto.confianca(CaProjeto.carregar(contexto!!).certificados, gerente)
        return OkHttpClient.Builder()
            .sslSocketFactory(fabrica, confianca)
            .connectTimeout(5, TimeUnit.SECONDS)
            .readTimeout(8, TimeUnit.SECONDS)
            .callTimeout(15, TimeUnit.SECONDS)
            .addInterceptor(auth!!.interceptor)
            .addInterceptor(recuperarCertificado)
            .authenticator(auth!!.authenticator)
            .build()
    }

    /**
     * Certificado de cliente recusado: erro de TLS que nao e do servidor, 401 certificado_ausente ou 403
     * certificado_incompativel. Pede um certificado novo em segundo plano (a chamada atual falha; a leitura seguinte
     * ja sai com o certificado novo, sem fila). A verificacao do servidor nunca e desligada.
     */
    private val recuperarCertificado = Interceptor { chain ->
        val e = emissor!!()
        val resposta = try {
            chain.proceed(chain.request())
        } catch (ex: IOException) {
            if (e.temCertificado() && falhaTlsPorCertificadoDeCliente(ex)) e.renovarEmSegundoPlano(descartar = true)
            throw ex
        }
        when (resposta.certificadoRecusado()) {
            "certificado_ausente" -> e.renovarEmSegundoPlano(descartar = false)
            "certificado_incompativel" -> e.renovarEmSegundoPlano(descartar = true)
        }
        resposta
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

/** "certificado_ausente" (401), "certificado_incompativel" (403) ou null: o que a borda diz sobre o certificado de cliente. */
fun Response.certificadoRecusado(): String? {
    if (code != 401 && code != 403) return null
    val corpo = try {
        peekBody(512).string()
    } catch (e: Exception) {
        return null
    }
    return erroDeCertificadoNoCorpo(code, corpo)
}
