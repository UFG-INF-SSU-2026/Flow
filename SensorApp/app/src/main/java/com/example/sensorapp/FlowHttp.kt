package com.example.sensorapp

import android.content.Context
import okhttp3.Interceptor
import okhttp3.OkHttpClient
import okhttp3.Response
import java.io.IOException
import java.util.concurrent.TimeUnit
import javax.net.ssl.KeyManager

/**
 * Cliente HTTP do app: telemetria, eventos de retorno e o canal SSE saem do [cliente]; as rotas de autenticacao
 * (/auth/..., que nao exigem certificado de cliente) saem do [semCertificado].
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

    /** Mesma confianca TLS do [cliente], mas sem apresentar certificado de cliente (ver [semCertificado]). */
    @Volatile
    private var baseSemCertificado: OkHttpClient? = null

    /** Cria o cliente base (idempotente). Chamado uma vez, na subida do processo ([FlowApp]). */
    @Synchronized
    fun iniciar(auth: AuthClient, contexto: Context, emissor: () -> EmissorCertificado) {
        if (base != null) return
        this.auth = auth
        this.contexto = contexto.applicationContext
        this.emissor = emissor
        base = construir()
        baseSemCertificado = construirSemCertificado()
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
        val antigoSem = baseSemCertificado
        base = construir()
        baseSemCertificado = construirSemCertificado()
        antigo?.connectionPool?.evictAll()
        antigoSem?.connectionPool?.evictAll()
    }

    private fun construir(): OkHttpClient {
        val gerente = emissor!!().gerenteDeChave()      // leitura sem trava: nao ha impasse com a emissao
        return construirTls(gerente)
            .addInterceptor(auth!!.interceptor)
            .addInterceptor(recuperarCertificado)
            .authenticator(auth!!.authenticator)
            .build()
    }

    /**
     * Cliente do pedido de certificado: so TLS e prazos, sem certificado de cliente, sem interceptores e sem
     * autenticador (o [EmissorCertificado] poe o token e renova uma vez por conta propria). A rota nao exige
     * certificado; assim um certificado guardado e recusado pelo servidor nao derruba o pedido do novo.
     */
    private fun construirSemCertificado(): OkHttpClient = construirTls(null).build()

    private fun construirTls(gerente: KeyManager?): OkHttpClient.Builder {
        val (fabrica, confianca) = CaProjeto.confianca(CaProjeto.carregar(contexto!!).certificados, gerente)
        return OkHttpClient.Builder()
            .sslSocketFactory(fabrica, confianca)
            .connectTimeout(5, TimeUnit.SECONDS)
            .readTimeout(8, TimeUnit.SECONDS)
            .callTimeout(15, TimeUnit.SECONDS)
    }

    /**
     * Certificado de cliente recusado: falha de conexao feita com certificado (com TLS 1.3 a recusa pode chegar como
     * conexao fechada, nao so como erro de TLS; ver [falhaDeConexaoComCertificado]), 401 certificado_ausente ou 403
     * certificado_incompativel. Pede um certificado novo em segundo plano (a chamada atual falha; a leitura seguinte
     * ja sai com o certificado novo, sem fila). O guardado so e trocado se a emissao der certo. A verificacao do
     * servidor nunca e desligada.
     */
    private val recuperarCertificado = Interceptor { chain ->
        val e = emissor!!()
        val resposta = try {
            chain.proceed(chain.request())
        } catch (ex: IOException) {
            if (e.temCertificado() && falhaDeConexaoComCertificado(ex)) e.renovarEmSegundoPlano()
            throw ex
        }
        if (resposta.certificadoRecusado() != null) e.renovarEmSegundoPlano()
        resposta
    }

    /** Para o pedido do certificado de cliente: sem certificado de cliente (a rota nao o exige). */
    val semCertificado: OkHttpClient
        get() = baseSemCertificado ?: error("FlowHttp.iniciar() nao foi chamado")

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
