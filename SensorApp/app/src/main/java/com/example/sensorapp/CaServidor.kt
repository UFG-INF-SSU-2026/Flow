package com.example.sensorapp

import okhttp3.HttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.security.cert.X509Certificate
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLContext
import javax.net.ssl.X509TrustManager

/**
 * Importar a CA direto do servidor (POST /auth/ca).
 *
 * Problema de partida: o app so confia na CA do projeto ([CaProjeto]); sem ela (ou com a CA antiga) nao ha como validar
 * o certificado do servidor para pedir a CA nova. Por isso ESTA chamada, e so ela, nao valida a cadeia nem o nome do
 * servidor. O que protege o pedido nao e o TLS, e o envelope AES-GCM do projeto ([CryptoUtils]):
 *  - a senha vai cifrada e autenticada no envelope: quem esta no meio do caminho nao a le;
 *  - a resposta tambem e um envelope: so quem tem a chave do envelope (o servidor) consegue produzir um que decifre,
 *    entao uma CA forjada por um impostor nao passa da decifragem;
 *  - a CA recebida ainda passa por [interpretarCa] (X.509, dentro do prazo, marca de CA, sem chave privada).
 * Nada daqui vai para o cliente HTTP do app ([FlowHttp]), que segue validando tudo; so a CA gravada o altera.
 *
 * [baixar] e BLOQUEANTE: chame fora da thread principal.
 */
object CaServidor {
    sealed class Resultado {
        /** [bytes] e o PEM validado, pronto para [CaProjeto.importar]. */
        class Ok(val bytes: ByteArray, val certificados: List<X509Certificate>) : Resultado()
        class Erro(val motivo: String) : Resultado()
    }

    private val jsonType = "application/json; charset=utf-8".toMediaType()

    /** Envelope de resposta cifrado e pequeno (a CA tem ~1 KB); acima disto nao e a resposta esperada. */
    private const val LIMITE_RESPOSTA = 4 * LIMITE_BYTES

    /** URL de https://ip:porta/auth/ca, ou null se IP ou porta nao servem. */
    fun url(ip: String, porta: String): HttpUrl? {
        val p = porta.trim().toIntOrNull() ?: return null
        if (p !in 1..65535 || ip.isBlank()) return null
        return try {
            HttpUrl.Builder().scheme("https").host(ip.trim()).port(p).addPathSegments("auth/ca").build()
        } catch (e: IllegalArgumentException) {
            null
        }
    }

    /** Mensagem para o codigo HTTP de falha (sem 2xx). */
    fun erroDoCodigoHttp(codigo: Int): String = when (codigo) {
        401 -> "Usuário ou senha inválidos."
        429 -> "Muitas tentativas. Aguarde alguns minutos."
        404 -> "Este servidor não entrega a CA (atualize o servidor)."
        503 -> "Servidor indisponível ou sem CA. Tente de novo em instantes."
        else -> "Resposta inesperada do servidor (HTTP $codigo)."
    }

    /** Corpo do pedido (texto claro, ainda por cifrar). A senha so existe aqui e no envelope; nunca e guardada. */
    fun corpoDoPedido(usuario: String, senha: String): JSONObject = JSONObject()
        .put("schemaVersion", 1)
        .put("identificador", usuario)
        .put("segredo", senha)
        .put("fonte", "app")

    /** Interpreta o texto claro (ja decifrado) da resposta: {"ca": "<PEM>"}. */
    fun interpretarResposta(claro: String): Resultado {
        val pem = try {
            JSONObject(claro).getString("ca")
        } catch (e: Exception) {
            return Resultado.Erro("Resposta do servidor sem CA.")
        }
        val bytes = pem.toByteArray(Charsets.US_ASCII)
        return when (val r = interpretarCa(bytes)) {
            is ResultadoCa.Ok -> Resultado.Ok(bytes, r.certificados)
            is ResultadoCa.Erro -> Resultado.Erro("CA recebida recusada: ${r.motivo}")
        }
    }

    fun baixar(ip: String, porta: String, usuario: String, senha: String): Resultado {
        val alvo = url(ip, porta) ?: return Resultado.Erro("IP ou porta inválidos.")
        if (usuario.isBlank() || senha.isEmpty()) return Resultado.Erro("Preencha usuário e senha.")
        return try {
            val (nonce, ct) = CryptoUtils.encrypt(corpoDoPedido(usuario.trim(), senha).toString())
            val envelope = JSONObject().put("nonce", nonce).put("ciphertext", ct)
            val req = Request.Builder().url(alvo).post(envelope.toString().toRequestBody(jsonType)).build()
            clienteSemConfianca().newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) return Resultado.Erro(erroDoCodigoHttp(resp.code))
                val bruto = resp.body?.byteStream()?.use { lerLimitado(it, LIMITE_RESPOSTA) } ?: ByteArray(0)
                if (bruto.size > LIMITE_RESPOSTA) return Resultado.Erro("Resposta grande demais.")
                val e = JSONObject(String(bruto, Charsets.UTF_8))
                val claro = CryptoUtils.decrypt(e.getString("nonce"), e.getString("ciphertext"))
                interpretarResposta(claro)
            }
        } catch (e: Exception) {
            Resultado.Erro("Falha de rede ou resposta inesperada: ${e.message}")
        }
    }

    /**
     * Cliente EXCLUSIVO desta chamada (ver a justificativa no topo): aceita qualquer certificado e nome de servidor, nao
     * segue redirecionamentos e nao leva token nem certificado de cliente. Nao e o [FlowHttp.cliente].
     */
    private fun clienteSemConfianca(): OkHttpClient {
        val aceitaTudo = object : X509TrustManager {
            override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) = Unit
            override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) = Unit
            override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
        }
        val contexto = SSLContext.getInstance("TLS").apply { init(null, arrayOf(aceitaTudo), null) }
        return OkHttpClient.Builder()
            .sslSocketFactory(contexto.socketFactory, aceitaTudo)
            .hostnameVerifier { _, _ -> true }
            .followRedirects(false)
            .followSslRedirects(false)
            .connectTimeout(5, TimeUnit.SECONDS)
            .readTimeout(8, TimeUnit.SECONDS)
            .callTimeout(15, TimeUnit.SECONDS)
            .build()
    }
}
